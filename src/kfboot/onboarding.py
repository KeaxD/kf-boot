from __future__ import annotations

import json

import falcon
from keri import help
from keri.app.httping import CESR_ATTACHMENT_HEADER, CESR_CONTENT_TYPE, CesrRequest
from keri.core import serdering
from keri.kering import (
    Ilks,
    MissingAuthAttachmentError,
    MissingSenderKeyStateError,
    MissingSignatureError,
    ValidationError,
)

from kfboot.boot_exchanger import ACCOUNT_ROUTES, ONBOARDING_ROUTES


EVENT_ILKS = {Ilks.icp, Ilks.rot, Ilks.ixn, Ilks.dip, Ilks.drt}
ACCEPTED_CESR_CONTENT_TYPES = {CESR_CONTENT_TYPE, "application/cesr+json"}
logger = help.ogler.getLogger(__name__)


class CesrSurfaceEnd:
    def __init__(self, ctx, *, surface: str):
        self.ctx = ctx
        self.surface = surface

    def on_post(self, req: falcon.Request, rep: falcon.Response) -> None:
        if req.method == "OPTIONS":
            rep.status = falcon.HTTP_200
            return

        rep.set_header("Cache-Control", "no-cache")

        cr = _parse_cesr_http_request(req=req, surface=self.surface)
        serder = serdering.SerderKERI(sad=cr.payload)
        self._validate_surface(serder)
        if serder.ilk == Ilks.exn and serder.pre not in self.ctx.habery.kevers:
            raise falcon.HTTPUnauthorized(
                title="Unknown sender key state",
                description=(
                    "The authenticated sender must send or precede the first business "
                    "message with inception or keystate material."
                ),
            )

        msg = bytearray(serder.raw)
        msg.extend(cr.attachments.encode("utf-8"))

        self.ctx.exchanger.set_client_ip(req.remote_addr or "")
        self.ctx.exchanger.expire_sessions()
        self.ctx.exchanger.clear_replies()

        try:
            self.ctx.parser.parseOne(ims=msg, exc=self.ctx.exchanger, local=False)
        except falcon.HTTPError:
            raise
        except (MissingAuthAttachmentError, MissingSenderKeyStateError, MissingSignatureError) as exc:
            raise falcon.HTTPUnauthorized(
                title="Authentication failed",
                description=str(exc),
            ) from exc
        except ValidationError as exc:
            raise falcon.HTTPBadRequest(
                title="Invalid CESR message",
                description=str(exc),
            ) from exc

        if serder.ilk in EVENT_ILKS:
            _require_accepted_keystate(habery=self.ctx.habery, serder=serder)

        reply = self.ctx.exchanger.take_reply()
        if reply is not None:
            rep.content_type = CESR_CONTENT_TYPE
            rep.data = reply
            rep.status = falcon.HTTP_200
            return

        if self.ctx.exchanger.last_error is not None:
            raise self.ctx.exchanger.last_error

        if serder.ilk in EVENT_ILKS:
            rep.status = falcon.HTTP_204
            return

        raise falcon.HTTPUnauthorized(
            title="Request rejected",
            description="The authenticated request was not accepted by the boot service.",
        )

    def _validate_surface(self, serder) -> None:
        if serder.ilk in EVENT_ILKS:
            return

        if serder.ilk != Ilks.exn:
            raise falcon.HTTPBadRequest(
                title="Unsupported message type",
                description=f"{serder.ilk} is not supported on the {self.surface} surface.",
            )

        route = str(serder.ked.get("r", "") or "")
        if self.surface == "onboarding" and route in ONBOARDING_ROUTES:
            return
        if self.surface == "account" and route in ACCOUNT_ROUTES:
            return

        raise falcon.HTTPNotFound(
            title="Unknown route",
            description=f"{route or '<missing>'} is not available on the {self.surface} surface.",
        )


def _parse_cesr_http_request(req: falcon.Request, *, surface: str) -> CesrRequest:
    raw_content_type = req.get_header("content-type", default="") or ""
    parsed_content_type = (req.content_type or "").strip().lower()
    content_type = parsed_content_type or raw_content_type
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type not in ACCEPTED_CESR_CONTENT_TYPES:
        logger.warning(
            (
                "Rejected CESR request on %s surface: content_type raw=%r "
                "parsed=%r media_type=%r accepted=%s path=%s remote=%s"
            ),
            surface,
            raw_content_type,
            parsed_content_type,
            media_type,
            sorted(ACCEPTED_CESR_CONTENT_TYPES),
            req.path,
            req.remote_addr or "",
        )
        raise falcon.HTTPError(
            falcon.HTTP_NOT_ACCEPTABLE,
            title="Content type error",
            description="Unacceptable content type.",
        )

    try:
        data = json.load(req.bounded_stream)
    except ValueError as exc:
        logger.warning(
            "Rejected CESR request on %s surface: malformed JSON path=%s remote=%s",
            surface,
            req.path,
            req.remote_addr or "",
        )
        raise falcon.HTTPBadRequest(
            title="Malformed JSON",
            description="Could not decode the request body. The JSON was incorrect.",
        ) from exc

    if CESR_ATTACHMENT_HEADER not in req.headers:
        logger.warning(
            "Rejected CESR request on %s surface: missing attachment header path=%s remote=%s",
            surface,
            req.path,
            req.remote_addr or "",
        )
        raise falcon.HTTPPreconditionFailed(
            title="Attachment error",
            description="Missing required attachment header.",
        )

    return CesrRequest(
        payload=data,
        attachments=req.headers[CESR_ATTACHMENT_HEADER],
    )


def _require_accepted_keystate(*, habery, serder) -> None:
    kever = habery.kevers.get(serder.pre)
    sn = int(getattr(serder, "sn", serder.ked.get("s", 0)) or 0)

    if kever is None or kever.sn < sn:
        raise falcon.HTTPConflict(
            title="Key state pending",
            description=(
                "The boot service has not accepted this key event yet. "
                "Submit the fully replayed event message, including required witness receipts, "
                "before sending authenticated requests."
            ),
        )

    accepted_said = getattr(getattr(kever, "serder", None), "said", "")
    if kever.sn == sn and accepted_said and accepted_said != serder.said:
        raise falcon.HTTPConflict(
            title="Key state superseded",
            description="The submitted key event does not match the boot service's accepted key state.",
        )
