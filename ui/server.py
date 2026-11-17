from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.router import chat


class AmikiHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path == "/":
            self.serve_index()
            return

        self.send_error(404, "Not Found")

    def do_POST(self):
        if self.path != "/api/chat":
            self.send_error(404, "Not Found")
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            data = json.loads(body.decode("utf-8"))

            message = data.get("message", "").strip()

            if not message:
                self.send_json({"error": "Message cannot be empty."}, 400)
                return

            response = chat(message)

            self.send_json({"response": response})

        except json.JSONDecodeError:
            self.send_json({"error": "Invalid JSON."}, 400)

        except Exception:
            self.send_json(
                {"error": "Something went wrong while processing your request."},
                500,
            )

    def serve_index(self):
        index_path = PROJECT_ROOT / "ui" / "index.html"

        try:
            content = index_path.read_bytes()

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        except FileNotFoundError:
            self.send_error(404, "index.html not found")
        except Exception:
            self.send_json(
                {"error": "Something went wrong while processing your request."},
                500,
            )

    def send_json(self, data, status=200):
        content = json.dumps(data).encode("utf-8")

        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


def main():
    server = HTTPServer(("127.0.0.1", 8000), AmikiHandler)

    print("🌸 Amiki UI server running")
    print("Open: http://127.0.0.1:8000")
    print("Press Ctrl+C to stop.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nAmiki UI server stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
