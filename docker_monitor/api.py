"""The control API the portfolio dashboard talks to.

**On adding FastAPI.** This repo's README makes a point of its one
third-party dependency, and this breaks that. The justification: the
service now needs routing, request validation, concurrent request handling
that doesn't block the poll loop, and Server-Sent Events. Hand-rolling
those on `http.server` is a few hundred lines of exactly the code
everyone gets subtly wrong (header parsing, chunked responses,
thread-per-connection). The poll loop itself is unchanged in character —
it just runs as an asyncio task inside the same process now, so this is
still one container doing one job.

**Two tiers of caller, and the boundary between them is the whole point of
this module.** A guest is anyone who reaches the endpoint without a token:
they can read everything, and start/stop only projects explicitly marked
`guest_controllable` in the registry, under a rate limit, a concurrency
cap and a lease that stops the project again whether or not they come
back. An owner presents `Authorization: Bearer $CONTROL_TOKEN` and skips
all of that, and is the only one who can `restart` or `update` anything.

Note what is *not* here: no endpoint takes a compose file path, a
container name, a command, or anything else that names something on the
host. Callers name a project; the project must already be in the registry;
the registry is operator-controlled and read-only to this process. That is
what keeps a public endpoint from turning into arbitrary control of the
NAS.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .ratelimit import resolve_client_ip
from .service import ControlError, MonitorService

logger = logging.getLogger("docker_monitor")

# How often an idle SSE connection gets a comment frame. Without this,
# proxies and load balancers close a quiet stream; nginx's default
# proxy_read_timeout is 60s.
HEARTBEAT_SECONDS = 15


def _client_ip(request: Request, cfg) -> str:
    fallback = request.client.host if request.client else "unknown"
    return resolve_client_ip(request.headers, fallback, cfg.trust_proxy_headers)


def create_app(service: MonitorService, cfg, poll: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        """Owns the poll loop for the lifetime of the server, so the two
        start and stop together — a server up but not polling would serve
        confidently stale project state."""
        poller = asyncio.create_task(poll_forever(service)) if poll else None
        try:
            yield
        finally:
            if poller is not None:
                poller.cancel()
                try:
                    await poller
                except asyncio.CancelledError:
                    pass
            service.shutdown()

    app = FastAPI(
        title="docker-monitor control API",
        description=(
            "Status and lifecycle control for the Compose projects on this host. "
            "See the repo README for the guest/owner split."
        ),
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )

    def require_owner(authorization: Optional[str] = Header(default=None)) -> bool:
        """Owner-only dependency. Refuses when no token is configured at
        all, rather than treating "no token" as "everyone is the owner" —
        the failure mode of the alternative is a public endpoint that can
        recreate containers."""
        if not cfg.control_token:
            raise HTTPException(
                status_code=503,
                detail="no CONTROL_TOKEN configured on this deployment — owner operations are disabled",
            )
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="owner token required")
        presented = authorization[len("Bearer ") :].strip()
        # Constant-time comparison: a plain != leaks the token
        # prefix-by-prefix to anyone who can measure response times.
        if not hmac.compare_digest(presented, cfg.control_token):
            raise HTTPException(status_code=403, detail="invalid owner token")
        return True

    def is_owner(authorization: Optional[str] = Header(default=None)) -> bool:
        """Soft variant for endpoints guests may also use: says whether the
        caller proved ownership, without refusing them if they didn't."""
        if not cfg.control_token or not authorization or not authorization.startswith("Bearer "):
            return False
        return hmac.compare_digest(authorization[len("Bearer ") :].strip(), cfg.control_token)

    def _handle(exc: ControlError) -> HTTPException:
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
        return HTTPException(status_code=exc.status, detail=str(exc), headers=headers)

    # -- read ------------------------------------------------------------

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.get("/api/projects")
    def list_projects() -> dict:
        return service.snapshot_payload()

    @app.get("/api/projects/{name}")
    def get_project(name: str) -> dict:
        view = service.view(name)
        if view is None:
            raise HTTPException(status_code=404, detail=f"unknown project {name!r}")
        payload = view.to_dict()
        payload["last_operation"] = service.last_result(name)
        return payload

    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        """Server-Sent Events: the current project list on connect, then
        again whenever anything meaningful changes, plus a heartbeat.

        The poll loop signals changes from its own thread, so the bridge
        between the two is an asyncio.Event set via `call_soon_threadsafe`.
        """
        loop = asyncio.get_running_loop()
        changed = asyncio.Event()

        def on_change() -> None:
            loop.call_soon_threadsafe(changed.set)

        service.subscribe(on_change)

        async def stream():
            try:
                yield _sse(service.snapshot_payload())
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        await asyncio.wait_for(changed.wait(), timeout=HEARTBEAT_SECONDS)
                        changed.clear()
                        yield _sse(service.snapshot_payload())
                    except asyncio.TimeoutError:
                        # Comment frame: keeps the connection alive without
                        # the client having to parse anything.
                        yield ": keep-alive\n\n"
            finally:
                service.unsubscribe(on_change)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                # nginx buffers proxied responses by default, which would
                # hold every event until the buffer fills — i.e. forever,
                # for a low-volume stream like this one.
                "X-Accel-Buffering": "no",
            },
        )

    # -- control ----------------------------------------------------------

    @app.post("/api/projects/{name}/start")
    def start(
        request: Request,
        name: str,
        payload: dict = Body(default=None),
        owner: bool = Depends(is_owner),
    ) -> dict:
        ttl = (payload or {}).get("ttl_minutes")
        if ttl is not None:
            try:
                ttl = int(ttl)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="ttl_minutes must be a number")
        try:
            return service.start_project(name, ttl, owner=owner, ip=_client_ip(request, cfg))
        except ControlError as exc:
            raise _handle(exc) from exc

    @app.post("/api/projects/{name}/stop")
    def stop(request: Request, name: str, owner: bool = Depends(is_owner)) -> dict:
        try:
            return service.stop_project(name, owner=owner, ip=_client_ip(request, cfg))
        except ControlError as exc:
            raise _handle(exc) from exc

    @app.post("/api/projects/{name}/restart")
    def restart(name: str, _: bool = Depends(require_owner)) -> dict:
        try:
            return service.restart_project(name)
        except ControlError as exc:
            raise _handle(exc) from exc

    @app.post("/api/projects/{name}/update")
    def update(name: str, _: bool = Depends(require_owner)) -> dict:
        try:
            return service.update_project(name)
        except ControlError as exc:
            raise _handle(exc) from exc

    @app.exception_handler(ControlError)
    def control_error_handler(_: Request, exc: ControlError) -> JSONResponse:  # pragma: no cover
        return JSONResponse(status_code=exc.status, content={"detail": str(exc)})

    return app


def _sse(payload: dict) -> str:
    return f"event: projects\ndata: {json.dumps(payload)}\n\n"


async def poll_forever(service: MonitorService) -> None:
    """The original `while True: poll; sleep` loop, as an asyncio task.

    The Docker SDK is synchronous and a poll can take seconds on a busy
    host, so the work goes to a worker thread — otherwise it would block
    every in-flight HTTP request and the SSE streams along with it.
    """
    while True:
        started = time.time()
        try:
            await asyncio.to_thread(service.poll_once)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error during poll cycle (will retry next interval)")
        delay = max(0.0, service.poll_interval() - (time.time() - started))
        await asyncio.sleep(delay)
