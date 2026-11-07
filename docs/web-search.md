# Web search setup

Danica uses SearXNG for `web_search`. It tries the configured primary server
first (`http://localhost:8888` by default), which is suitable for a local
Docker instance. If that server cannot be reached, it tries the optional
fallback URL (`https://search.utilibre.org` by default).

Override either URL in `%APPDATA%\Danica\config.toml`:

```toml
[web]
search_url = "http://localhost:8888"
search_fallback_url = "https://search.utilibre.org"
```

Set `search_fallback_url = ""` to disable remote fallback and use only the
primary server. The fallback is triggered on connection failure; HTTP errors
such as a server refusing JSON output are reported instead of silently
switching services.

When fallback is used, the search query is sent to the hosted SearXNG server,
which may forward it to public search engines. Do not search for credentials,
private personal information, or other sensitive text. Danica blocks
secret-looking queries before making either request.

The configured SearXNG instance must expose `/search` with JSON format enabled.
If the hosted instance does not allow JSON search, Danica reports that error;
it will not switch hosts for that configuration error.
