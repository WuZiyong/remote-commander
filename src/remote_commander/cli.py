"""Command line entry point. All diagnostics go to stderr for stdio MCP."""

import argparse
import logging
import os
import secrets
import sys

import uvicorn
from dotenv import load_dotenv

from .config import AgentConfig, GatewayConfig, read_config
from .runtime import Gateway, Runtime
from .security import Guard
from .server import create_server


def main():
    parser = argparse.ArgumentParser(description="Self-hosted multi-device MCP tools")
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser(
        "token", help="Generate a random token; save it in a secret store or local env file"
    )
    for name in ["agent", "gateway"]:
        command = sub.add_parser(name)
        command.add_argument("--config", required=True)
        command.add_argument(
            "--env-file", help="Optional private .env file containing RC_* secrets"
        )
        command.add_argument(
            "--transport", choices=["stdio", "http"], default="http" if name == "agent" else "stdio"
        )
        command.add_argument("--host", default="127.0.0.1")
        command.add_argument("--port", type=int, default=8765)
        command.add_argument(
            "--allowed-host",
            action="append",
            default=[],
            help="Allowed HTTP Host (repeatable); include port or :* explicitly",
        )
        command.add_argument("--token-env", default="RC_TOKEN")
    args = parser.parse_args()
    if args.mode == "token":
        print(secrets.token_urlsafe(48))
        return
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    try:
        if not args.token_env.startswith("RC_"):
            raise ValueError("--token-env must start with RC_")
        if args.env_file:
            from dotenv import dotenv_values

            if any(not key.startswith("RC_") for key in dotenv_values(args.env_file)):
                raise ValueError("Secret env files may only contain RC_ prefixed keys")
            load_dotenv(args.env_file, override=False)
        if args.mode == "agent":
            config = read_config(args.config, AgentConfig)
            runtime = Runtime(config)
        else:
            config = read_config(args.config, GatewayConfig)
            # Fail fast on missing secrets; do not create an unusable MCP gateway.
            for device in config.devices:
                device.token()
            runtime = Gateway(config)
        hosts = args.allowed_host or ["127.0.0.1:*", "localhost:*", "[::1]:*"]
        mcp = create_server(runtime, hosts)
        if args.transport == "stdio":
            mcp.run(transport="stdio")
        else:
            app = Guard(
                mcp.streamable_http_app(),
                os.environ.get(args.token_env, ""),
                max_body_bytes=(config.max_file_bytes * 2 + 65536)
                if isinstance(config, AgentConfig)
                else 4 * 1024 * 1024,
            )
            uvicorn.run(
                app,
                host=args.host,
                port=args.port,
                timeout_keep_alive=10,
                timeout_graceful_shutdown=5,
                limit_concurrency=64,
                timeout_notify=30,
            )
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Configuration/startup error: {exc}\n")


if __name__ == "__main__":
    main()
