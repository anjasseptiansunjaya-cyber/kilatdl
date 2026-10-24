#!/usr/bin/env python3
"""Make a shareable copy of a HAR file: strips cookies, auth headers, tokens and bodies.

Usage: har_sanitize.py capture.har        -> writes capture.sanitized.har (original untouched)

Kept for diagnosis: URLs, query parameter NAMES, status codes, Range/Content-* headers,
sizes and timings. Removed: Cookie/Set-Cookie/Authorization headers, cookie arrays,
ALL query parameter values (a one-letter name like t= can be a token), request/response bodies.
"""
import json
import re
import sys
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit

DROP_HEADERS = re.compile(r"^(cookie|set-cookie|authorization|proxy-authorization|x-.*(token|auth|key|csrf).*|.*-token)$", re.I)
URL_HEADERS = re.compile(r"^(referer|location|origin|content-location)$", re.I)
MARK = "REDACTED"


def clean_url(url):
    parts = urlsplit(url)
    pairs = [(k, MARK) for k, _ in parse_qsl(parts.query, keep_blank_values=True)]
    query = "&".join(f"{quote(k)}={quote(v, safe='')}" for k, v in pairs)
    userless = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parts.scheme, userless, parts.path, query, ""))


def clean_headers(headers):
    out = []
    for h in headers:
        if DROP_HEADERS.match(h["name"]):
            h = {"name": h["name"], "value": MARK}
        elif URL_HEADERS.match(h["name"]) and "://" in h["value"]:
            h = {"name": h["name"], "value": clean_url(h["value"])}
        out.append(h)
    return out


def clean_query(items):
    return [{"name": q["name"], "value": MARK} for q in items]


def sanitize(har):
    for entry in har["log"]["entries"]:
        req, resp = entry["request"], entry["response"]
        req["url"] = clean_url(req["url"])
        req["headers"] = clean_headers(req.get("headers", []))
        req["queryString"] = clean_query(req.get("queryString", []))
        req["cookies"] = []
        if "postData" in req:
            req["postData"] = {"mimeType": req["postData"].get("mimeType", ""), "text": MARK}
        resp["headers"] = clean_headers(resp.get("headers", []))
        resp["cookies"] = []
        if "redirectURL" in resp and resp["redirectURL"]:
            resp["redirectURL"] = clean_url(resp["redirectURL"])
        content = resp.get("content", {})
        content.pop("text", None)  # response bodies can hold tokens and personal data
        content.pop("encoding", None)
        entry.pop("_securityDetails", None)
        entry.pop("serverIPAddress", None)
    har["log"].pop("pages", None)
    return har


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    src = sys.argv[1]
    out = re.sub(r"(\.har)?$", ".sanitized.har", src, count=1)
    with open(src, encoding="utf-8") as f:
        har = json.load(f)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(sanitize(har), f, indent=1)
    print(f"Selesai: {out}\nPeriksa dulu isinya sebelum dibagikan; yang asli tidak diubah.")


if __name__ == "__main__":
    main()
