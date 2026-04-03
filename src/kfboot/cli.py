from __future__ import annotations

from wsgiref.simple_server import make_server

from kfboot.app import create_app


def main() -> None:
    app, ctx = create_app()
    server = make_server(ctx.config.host, ctx.config.port, app)
    try:
        print(f"kf-boot listening on http://{ctx.config.host}:{ctx.config.port}")
        server.serve_forever()
    finally:
        ctx.store.close()


if __name__ == "__main__":
    main()
