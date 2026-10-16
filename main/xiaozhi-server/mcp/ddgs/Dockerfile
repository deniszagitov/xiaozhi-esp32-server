FROM python:3.10-slim

ARG DDGS_VERSION=9.14.4
ARG MCP_PROXY_VERSION=0.10.0
ARG MCP_VERSION=1.28.1

RUN pip install --no-cache-dir \
    "ddgs[mcp]==${DDGS_VERSION}" \
    "mcp-proxy==${MCP_PROXY_VERSION}" \
    "mcp==${MCP_VERSION}"

EXPOSE 8000

CMD ["mcp-proxy", "--host", "0.0.0.0", "--port", "8000", "--stateless", "--", "ddgs", "mcp"]
