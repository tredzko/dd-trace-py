import functools
import logging
import sys

from ddtrace import config
from ddtrace.vendor import wrapt
from ddtrace.internal import import_hooks

from ... import constants as const
from ...utils.wrappers import unwrap
from ...propagation.http import HTTPPropagator
from ...pin import Pin
from ...ext import http as ext_http, SpanTypes
from ..httplib.patch import should_skip_request

from yarl import URL

_w = wrapt.wrap_function_wrapper

log = logging.getLogger(__name__)

propagator = HTTPPropagator()


def with_modules(*mods):
    def f(func):
        def wrapper(wrapped, instance, args, kwargs):
            if hasattr(func, "__dd_cached"):
                resolved_mods, pin = getattr(func, "__dd_cached")
            else:
                try:
                    resolved_mods = [sys.modules[mod] for mod in mods]
                except KeyError:
                    return wrapped(*args, **kwargs)
                else:
                    pin = Pin._find(*resolved_mods, instance)
                    if not pin or not pin.enabled():
                        return wrapped(*args, **kwargs)
                    setattr(func, "__dd_cached", (resolved_mods, pin))
            return func(*resolved_mods, pin, pin._config, wrapped, instance, args, kwargs)
        return wrapper
    return f


def _trace_render_template(func, module, args, kwargs):
    """
    Trace the template rendering
    """
    # get the module pin
    pin = Pin.get_from(aiohttp_jinja2)
    if not pin or not pin.enabled():
        return func(*args, **kwargs)

    # original signature:
    # render_template(template_name, request, context, *, app_key=APP_KEY, encoding='utf-8')
    template_name = args[0]
    request = args[1]
    env = aiohttp_jinja2.get_env(request.app)

    # the prefix is available only on PackageLoader
    template_prefix = getattr(env.loader, 'package_path', '')
    template_meta = '{}/{}'.format(template_prefix, template_name)

    with pin.tracer.trace('aiohttp.template', span_type=SpanTypes.TEMPLATE) as span:
        span.set_meta('aiohttp.template', template_meta)
        return func(*args, **kwargs)


def _get_url_obj(obj):
    url_obj = obj.url

    if not isinstance(url_obj, URL):
        url_obj = getattr(obj, 'url_obj', None)  # 1.x

    return url_obj


def _set_request_tags(span, url):
    if (url.scheme == 'http' and url.port == 80) or (url.scheme == 'https' and url.port == 443):
        port = ''
    else:
        port = ':{}'.format(url.port)

    url_str = '{scheme}://{host}{port}{path}'.format(scheme=url.scheme, host=url.host, port=port, path=url.path)

    span.set_tag(ext_http.URL, url_str)
    span.resource = url.path


class _WrappedConnectorClass(wrapt.ObjectProxy):
    def __init__(self, obj, pin):
        super().__init__(obj)
        pin.onto(self)

    async def connect(self, req, *args, **kwargs):
        pin = Pin.get_from(self)
        with pin.tracer.trace('{}.connect'.format(self.__class__.__name__),
                              span_type=ext_http.TYPE, service=pin.service) as span:
            _set_request_tags(span, _get_url_obj(req))
            # We call this way so "self" will not get sliced and call
            # _create_connection on us first
            result = await self.__wrapped__.__class__.connect(self, req, *args, **kwargs)
            return result

    async def _create_connection(self, req, *args, **kwargs):
        pin = Pin.get_from(self)
        with pin.tracer.trace(
                '{}._create_connection'.format(self.__class__.__name__),
                span_type=ext_http.TYPE, service=pin.service) as span:
            _set_request_tags(span, _get_url_obj(req))
            result = await self.__wrapped__._create_connection(req, *args, **kwargs)
            return result


class _WrappedResponseClass(wrapt.ObjectProxy):
    def __init__(self, obj, pin):
        super().__init__(obj)

        pin.onto(self)

        # We'll always have a parent span from outer request
        ctx = pin.tracer.get_call_context()
        parent_span = ctx.get_current_span()
        if parent_span:
            self._self_parent_trace_id = parent_span.trace_id
            self._self_parent_span_id = parent_span.span_id
        else:
            self._self_parent_trace_id, self._self_parent_span_id = ctx.trace_id, ctx.span_id

    async def start(self, *args, **kwargs):
        # This will get called once per connect
        pin = Pin.get_from(self)

        # This will parent correctly as we'll always have an enclosing span
        with pin.tracer.trace('{}.start'.format(self.__class__.__name__),
                              span_type=ext_http.TYPE, service=pin.service) as span:
            _set_request_tags(span, _get_url_obj(self))

            resp = await self.__wrapped__.start(*args, **kwargs)

            if pin._config["trace_headers"]:
                tags = {hdr: resp.headers[hdr]
                        for hdr in self._self_trace_headers
                        if hdr in resp.headers}
                span.set_tags(tags)

            span.set_tag(ext_http.STATUS_CODE, self.status)
            span.set_tag(ext_http.METHOD, resp.method)

        return resp

    async def read(self, *args, **kwargs):
        pin = Pin.get_from(self)
        # This may not have an immediate parent as the request completed
        with pin.tracer.trace('{}.read'.format(self.__class__.__name__),
                              span_type=ext_http.TYPE, service=pin.service) as span:

            if self._self_parent_trace_id:
                span.trace_id = self._self_parent_trace_id

            if self._self_parent_span_id:
                span.parent_id = self._self_parent_span_id

            _set_request_tags(span, _get_url_obj(self))
            result = await self.__wrapped__.read(*args, **kwargs)
            span.set_tag(ext_http.STATUS_CODE, self.status)
            span.set_tag('Length', len(result))

        return result

    async def __aenter__(self):
        result = await self.__wrapped__.__aenter__()
        return result

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        result = await self.__wrapped__.__aexit__(exc_type, exc_val, exc_tb)
        return result


class _WrappedRequestContext(wrapt.ObjectProxy):
    def __init__(self, obj, pin, span):
        super().__init__(obj)
        pin.onto(self)
        self._self_span = span
        self._self_have_context = False

    async def _handle_response(self, coro):
        pin = Pin.get_from(self)
        try:
            resp = await coro

            if pin._config["trace_headers"]:
                tags = {hdr: resp.headers[hdr]
                        for hdr in pin._config["trace_headers"]
                        if hdr in resp.headers}
                self._self_span.set_tags(tags)

            self._self_span.set_tag(ext_http.STATUS_CODE, resp.status)
            self._self_span.error = int(500 <= resp.status)
            return resp
        except BaseException:
            self._self_span.set_traceback()
            raise
        finally:
            if not self._self_have_context or not pin._config["trace_context"]:
                self._self_span.finish()

    # this will get when called without a context
    def __iter__(self):
        return self.__await__()

    def __await__(self):
        resp = self._handle_response(self.__wrapped__).__await__()
        return resp

    async def __aenter__(self):
        self._self_have_context = True
        resp = await self._handle_response(self.__wrapped__.__aenter__())
        return resp

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        try:
            resp = await self.__wrapped__.__aexit__(exc_type, exc_val, exc_tb)
        finally:
            if self._self_have_context and self._self_trace_context:
                self._self_span.finish()
        return resp


def _create_wrapped_request(method, func, instance, args, kwargs):
    pin = Pin.get_from(instance)

    if not pin.tracer.enabled:
        return func(*args, **kwargs)

    if method == "REQUEST":
        method = kwargs.get("method", args[0])
        url = URL(kwargs.get("url", args[1]))
    else:
        url = URL(kwargs.get("url", args[0]))

    if should_skip_request(pin, url):
        result = func(*args, **kwargs)
        return result

    # Create a new span and attach to this instance (so we can
    # retrieve/update/close later on the response)
    # Note that we aren't tracing redirects
    span = pin.tracer.trace('ClientSession.request', span_type=ext_http.TYPE, service=pin.service)

    if pin._config["distributed_tracing_enabled"]:
        headers = kwargs.get('headers', {})
        if headers is None:
            headers = {}
        propagator.inject(span.context, headers)
        kwargs["headers"] = headers

    _set_request_tags(span, url)
    span.set_tag(ext_http.METHOD, method)

    obj = _WrappedRequestContext(func(*args, **kwargs), pin, span)
    return obj


def _create_wrapped_response(client_session, cls, instance, args, kwargs):
    obj = _WrappedResponseClass(cls(*args, **kwargs), Pin.get_from(client_session))
    return obj


def _wrap_clientsession_init(func, instance, args, kwargs):
    # Use any attached tracer if available, otherwise use the global tracer
    pin = Pin.get_from(instance)

    if not pin.tracer.enabled:
        return func(*args, **kwargs)

    # note init doesn't really return anything
    ret = func(*args, **kwargs)

    # replace properties with our wrappers
    wrapper = functools.partial(_create_wrapped_response, instance)
    instance._response_class = wrapt.FunctionWrapper(instance._response_class, wrapper)

    instance._connector = _WrappedConnectorClass(instance._connector, pin)
    return ret


_clientsession_wrap_methods = {
    'get', 'options', 'head', 'post', 'put', 'patch', 'delete', 'request'
}


@with_modules("aiohttp", "aiohttp.web_app")
def traced__handle(aiohttp, aiohttp_web_app, pin, cfg, wrapped, instance, args, kwargs):
    with pin.tracer.trace("aiohttp.request", service=pin.service, span_type=SpanTypes.WEB) as span:
        span.set_tag(const.SPAN_MEASURED_KEY)
        # Configure trace search sample rate
        analytics_enabled = cfg["analytics_enabled"]
        if (config.analytics_enabled and analytics_enabled is not False) or analytics_enabled is True:
            span.set_tag(const.ANALYTICS_SAMPLE_RATE_KEY, cfg.get("analytics_sample_rate", True))
        return wrapped(*args, **kwargs)


def patch_aiohttp_web_app(aiohttp_web_app):
    Pin(
        config.aiohttp_server.service, app="aiohttp", _config=config.aiohttp_server
    ).onto(aiohttp_web_app.Application)
    _w(aiohttp_web_app, "Application._handle", traced__handle)


def patch_aiohttp(aiohttp):
    # Patch the http client.
    Pin(
        config.aiohttp_client.service, app="aiohttp", _config=config.aiohttp_client
    ).onto(aiohttp.ClientSession)

    _w("aiohttp", "ClientSession.__init__", _wrap_clientsession_init)

    for method in _clientsession_wrap_methods:
        wrapper = functools.partial(_create_wrapped_request, method.upper())
        _w('aiohttp', 'ClientSession.{}'.format(method), wrapper)

    # Patch the http server.
    import_hooks.register_module_hook("aiohttp.web_app", patch_aiohttp_web_app)


def patch_aiohttp_jinja2(aiohttp_jinja2):
    if not getattr(aiohttp_jinja2, '__datadog_patch', False):
        setattr(aiohttp_jinja2, '__datadog_patch', True)

        _w('aiohttp_jinja2', 'render_template', _trace_render_template)
        Pin(config.aiohttp_jinja2.service, app="aiohttp", _config=config.aiohttp_jinja2).onto(aiohttp_jinja2)


def patch():
    _w = wrapt.wrap_function_wrapper
    import aiohttp

    if not getattr(aiohttp, '__datadog_patch', False):
        setattr(aiohttp, '__datadog_patch', True)
        patch_aiohttp(aiohttp)

    try:
        import aiohttp_jinja2
    except ImportError:
        pass
    else:
        if not getattr(aiohttp_jinja2, '__datadog_patch', False):
            setattr(aiohttp_jinja2, '__datadog_patch', True)
            patch_aiohttp_jinja2(aiohttp_jinja2)


def unpatch():
    """
    Remove tracing from patched modules.
    """
    if getattr(aiohttp, '__datadog_patch', False):
        unwrap(aiohttp.ClientSession, '__init__')

        for method in _clientsession_wrap_methods:
            unwrap(aiohttp.ClientSession, method)

        setattr(aiohttp, '__datadog_patch', False)

    if _trace_render_template and getattr(aiohttp_jinja2, '__datadog_patch', False):
        setattr(aiohttp_jinja2, '__datadog_patch', False)
        unwrap(aiohttp_jinja2, 'render_template')


