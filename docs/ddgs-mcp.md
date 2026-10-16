# Optional DDGS search MCP service

The DDGS Compose override adds a web and news search MCP service. It does not
require an API key. Search queries are sent to external search providers.

From `main/xiaozhi-server`, start the existing full deployment with the override:

```sh
docker compose -f docker-compose_all.yml -f docker-compose.mcp.yml up -d --build
```

Merge this synthetic example into the private mounted
`data/.mcp_server_settings.json` without replacing other configured servers:

```json
{
  "mcpServers": {
    "ddgs": {
      "transport": "streamable-http",
      "url": "http://ddgs-mcp:8000/mcp"
    }
  }
}
```

Restart the voice server after changing its MCP configuration. The service uses
the Compose network and does not publish a host port. Credentials for any other
MCP service belong only in the private mounted configuration.

The image builds from `main/xiaozhi-server/mcp/ddgs/Dockerfile`, with explicit DDGS
and MCP proxy versions. Its TCP health check confirms that the listener is up;
validate MCP initialization and a search call separately before relying on it.
The override can be omitted to keep the existing deployment unchanged.
