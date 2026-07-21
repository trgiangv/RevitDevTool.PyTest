# Technical debt — pytest MCP request-id coupling

`RevitDevTool.PyTest` (`mcp_client.PytestClientSession`) reads the MCP Python
SDK private attribute `ClientSession._request_id` to:

1. Bind progress tokens for `pytest_run`
2. Issue `notifications/cancelled` for the active call

There is no public SDK API (as of the pin used by this plugin) that exposes the
next outbound JSON-RPC request id. Do **not** add another private-attribute
workaround. Track upgrading when upstream publishes an equivalent public hook;
until then treat SDK upgrades as a compatibility gate for this adapter.
