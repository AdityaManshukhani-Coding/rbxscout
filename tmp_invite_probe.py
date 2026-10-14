import json, socket, ssl, urllib.parse

INVITE = "https://discord.gg/emekjt2PY"
code = urllib.parse.urlparse(INVITE).path.strip("/")

base = "discord.com"
path = f"/api/v10/invites/{code}"

ctx = ssl.create_default_context()
sock = socket.create_connection((base, 443), timeout=10)
ssock = ctx.wrap_socket(sock, server_hostname=base)

req = (
    "GET " + path + " HTTP/1.1\r\n"
    "Host: " + base + "\r\n"
    "Accept: application/json\r\n"
    "User-Agent: okhttp/4.12.0\r\n"
    "Connection: close\r\n"
    "\r\n"
)
ssock.sendall(req.encode())

raw = b""
while True:
    c = ssock.recv(8192)
    if not c:
        break
    raw += c

# strip HTTP headers
he = raw.find(b"\r\n\r\n")
assert he != -1, "no header boundary"
body_start = he + 4
post_headers = raw[body_start:]

# chunked reader on the static buffer
out = b""
i = 0
n = len(post_headers)
if n >= 2 and post_headers[0:2] == b"\r\n":
    i = 2
while i < n:
    crlf = post_headers.find(b"\r\n", i)
    if crlf == -1:
        break
    hex_line = post_headers[i:crlf].decode(errors="replace").strip()
    i = crlf + 2
    try:
        size = int(hex_line, 16)
    except ValueError:
        break
    if size == 0:
        break
    end = i + size
    if end > n:
        break
    out += post_headers[i:end]
    i = end
    if i + 2 <= n and post_headers[i:i + 2] == b"\r\n":
        i += 2

print("chunked-body-LEN", len(out))
print(json.dumps(json.loads(out.decode(errors="replace")), indent=2))
