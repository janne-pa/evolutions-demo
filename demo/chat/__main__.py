import argparse

import uvicorn

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Loopback-only Apple Chat")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("--port must be between 1024 and 65535")
    uvicorn.run("demo.chat.main:create_app", factory=True, host="127.0.0.1", port=args.port)
