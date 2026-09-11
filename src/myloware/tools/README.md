Type: Reference
Audience: Technical

# Creating Custom Tools

AISMR tools extend `ClientTool` from Llama Stack, wrapped as `MylowareBaseTool` which provides:
- Standardized error handling
- JSON response formatting
- Observability integration

## Llama Stack Native Pattern

Tools follow Llama Stack's `ClientTool` pattern from `llama_stack_client.lib.agents.client_tool`.

Compatible with **llama-stack-client >= 0.3.x**.

## Steps to Create a New Tool

1. Create a new file in `src/myloware/tools/`
2. Extend `MylowareBaseTool`
3. Implement required methods:
   - `get_name()` → str
   - `get_description()` → str
   - `get_input_schema()` → JSONSchema (JSON Schema dict)
   - `run_impl(**kwargs)` → Dict[str, Any]
4. Add the tool to `src/myloware/tools/__init__.py` exports.

## Example

```python
from myloware.tools.base import JSONSchema, MylowareBaseTool, format_tool_success


class MyNewTool(MylowareBaseTool):
    def __init__(self, api_key: str):
        self.api_key = api_key

    def get_name(self) -> str:
        return "my_new_tool"

    def get_description(self) -> str:
        return "Description of what this tool does"

    def get_input_schema(self) -> JSONSchema:
        return {
            "type": "object",
            "properties": {
                "param1": {
                    "type": "string",
                    "description": "First parameter",
                },
                "count": {
                    "type": "integer",
                    "description": "Number of items",
                    "default": 10,
                },
            },
            "required": ["param1"],
        }

    def run_impl(self, param1: str, count: int = 10) -> dict:
        # Call external API
        result = {"param1": param1, "count": count}
        return format_tool_success(result, "Operation completed")
```

## JSON Schema Types

Use standard JSON Schema types:
- `"string"` - String values
- `"integer"` - Integer values
- `"number"` - Float/decimal values
- `"boolean"` - Boolean values
- `"array"` - Arrays (with `items` schema)
- `"object"` - Nested objects (with `properties`)

## Adding Fake Provider Support

For testing without real API calls, use a per-tool provider mode (e.g., `SORA_PROVIDER`, `REMOTION_PROVIDER`):

```python
def __init__(self, provider_mode: str | None = None):
    self.provider_mode = provider_mode or getattr(settings, "my_tool_provider", "real")
    if self.provider_mode == "off":
        raise ValueError("Provider disabled")

async def run_impl(self, **kwargs):
    if self.provider_mode == "fake":
        return self._fake_response()
    return await self._real_api_call(**kwargs)
```
