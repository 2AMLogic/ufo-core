"""The JSON shape an extension's stored data takes: spec fields, filters, tool input."""

type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]
