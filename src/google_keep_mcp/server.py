import argparse
import json
import logging
import os
import re
from typing import Any

import gkeepapi
from dotenv import load_dotenv
from fastmcp import FastMCP
from gkeepapi.node import ColorValue

load_dotenv()

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

mcp = FastMCP(
    name="Google Keep",
    version="0.1.0",
    instructions="MCP server for managing Google Keep notes, lists, and labels",
)

_keep: gkeepapi.Keep | None = None
_state_path: str | None = None


def _get_keep() -> gkeepapi.Keep:
    """Get an authenticated Keep instance, reusing cached state when available."""
    global _keep, _state_path

    if _keep is not None:
        return _keep

    email = os.environ.get("GOOGLE_KEEP_EMAIL")
    master_token = os.environ.get("GOOGLE_KEEP_MASTER_TOKEN")
    device_id = os.environ.get("GOOGLE_KEEP_DEVICE_ID")
    _state_path = os.environ.get("GOOGLE_KEEP_STATE_PATH")

    if not email or not master_token:
        raise RuntimeError(
            "Set GOOGLE_KEEP_EMAIL and GOOGLE_KEEP_MASTER_TOKEN environment variables"
        )

    keep = gkeepapi.Keep()

    # Restore cached state if available for faster startup
    saved_state = None
    if _state_path and os.path.exists(_state_path):
        with open(_state_path) as f:
            saved_state = json.load(f)

    keep.authenticate(email, master_token, state=saved_state, device_id=device_id)

    _keep = keep
    return _keep


def _save_state() -> None:
    """Persist Keep state to disk for faster future startups."""
    if _keep and _state_path:
        with open(_state_path, "w") as f:
            json.dump(_keep.dump(), f)


def _sync() -> None:
    """Sync changes with Google servers and persist state."""
    keep = _get_keep()
    keep.sync()
    _save_state()


def _note_to_dict(note: gkeepapi.node.TopLevelNode) -> dict[str, Any]:
    """Serialize a note/list to a dictionary."""
    result: dict[str, Any] = {
        "id": note.id,
        "title": note.title,
        "type": note.type.name,
        "color": note.color.name,
        "pinned": note.pinned,
        "archived": note.archived,
        "trashed": note.trashed,
        "url": note.url,
        "labels": [label.name for label in note.labels.all()],
    }

    if isinstance(note, gkeepapi.node.List):
        result["items"] = [
            {
                "id": item.id,
                "text": item.text,
                "checked": item.checked,
            }
            for item in note.items
        ]
    else:
        result["text"] = note.text or ""

    return result


COLOR_MAP = {c.name.lower(): c for c in ColorValue}


def _parse_color(color: str) -> ColorValue:
    """Parse a color string to a ColorValue enum."""
    key = color.strip().lower()
    if key in COLOR_MAP:
        return COLOR_MAP[key]
    raise ValueError(
        f"Unknown color '{color}'. Valid colors: {', '.join(COLOR_MAP.keys())}"
    )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def search_notes(
    query: str = "",
    labels: list[str] | None = None,
    color: str | None = None,
    pinned: bool | None = None,
    archived: bool | None = None,
    trashed: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Search Google Keep notes and lists.

    Args:
        query: Text to search in title and body (supports regex).
        labels: Filter by label names.
        color: Filter by color name (e.g. "yellow", "blue").
        pinned: Filter by pinned status.
        archived: Filter by archived status.
        trashed: Include trashed notes.
        limit: Maximum number of results to return.
    """
    keep = _get_keep()

    label_objects = None
    if labels:
        label_objects = []
        for name in labels:
            label = keep.findLabel(name)
            if label:
                label_objects.append(label)

    color_value = None
    if color:
        color_value = [_parse_color(color)]

    try:
        pattern = re.compile(query, re.IGNORECASE) if query else None
    except re.error as e:
        raise ValueError(f"Invalid regex in query: {e}")
    results = keep.find(
        query=pattern,
        labels=label_objects,
        colors=color_value,
        pinned=pinned,
        archived=archived,
        trashed=trashed,
    )

    return [_note_to_dict(n) for _, n in zip(range(limit), results)]


@mcp.tool()
def get_note(note_id: str) -> dict[str, Any]:
    """Get a single note or list by its ID.

    Args:
        note_id: The unique ID of the note.
    """
    keep = _get_keep()
    note = keep.get(note_id)
    if note is None:
        raise ValueError(f"Note not found: {note_id}")
    return _note_to_dict(note)


@mcp.tool()
def create_note(
    title: str = "",
    text: str = "",
    color: str | None = None,
    pinned: bool = False,
    labels: list[str] | None = None,
) -> dict[str, Any]:
    """Create a new text note in Google Keep.

    Args:
        title: Note title.
        text: Note body text.
        color: Note color (e.g. "yellow", "blue", "green").
        pinned: Whether to pin the note.
        labels: Label names to apply (created if they don't exist).
    """
    keep = _get_keep()
    note = keep.createNote(title, text)

    if color:
        note.color = _parse_color(color)
    note.pinned = pinned

    if labels:
        for name in labels:
            label = keep.findLabel(name, create=True)
            note.labels.add(label)

    _sync()
    return _note_to_dict(note)


@mcp.tool()
def create_list(
    title: str = "",
    items: list[dict[str, Any]] | None = None,
    color: str | None = None,
    pinned: bool = False,
    labels: list[str] | None = None,
) -> dict[str, Any]:
    """Create a new checklist in Google Keep.

    Args:
        title: List title.
        items: List items as objects with "text" and optional "checked" boolean.
               Example: [{"text": "Buy milk", "checked": false}, {"text": "Buy eggs"}]
        color: List color (e.g. "yellow", "blue").
        pinned: Whether to pin the list.
        labels: Label names to apply (created if they don't exist).
    """
    keep = _get_keep()

    list_items = []
    if items:
        list_items = [(i["text"], i.get("checked", False)) for i in items]

    note = keep.createList(title, list_items)

    if color:
        note.color = _parse_color(color)
    note.pinned = pinned

    if labels:
        for name in labels:
            label = keep.findLabel(name, create=True)
            note.labels.add(label)

    _sync()
    return _note_to_dict(note)


@mcp.tool()
def update_note(
    note_id: str,
    title: str | None = None,
    text: str | None = None,
    color: str | None = None,
    pinned: bool | None = None,
    archived: bool | None = None,
) -> dict[str, Any]:
    """Update an existing note's properties.

    Args:
        note_id: The ID of the note to update.
        title: New title (omit to keep current).
        text: New body text (omit to keep current; only for text notes).
        color: New color name.
        pinned: New pinned status.
        archived: New archived status.
    """
    keep = _get_keep()
    note = keep.get(note_id)
    if note is None:
        raise ValueError(f"Note not found: {note_id}")

    if title is not None:
        note.title = title
    if text is not None:
        if isinstance(note, gkeepapi.node.List):
            raise ValueError(
                "Cannot set text on a list/checklist; use add_list_item/update_list_item instead"
            )
        note.text = text
    if color is not None:
        note.color = _parse_color(color)
    if pinned is not None:
        note.pinned = pinned
    if archived is not None:
        note.archived = archived

    _sync()
    return _note_to_dict(note)


@mcp.tool()
def trash_note(note_id: str) -> str:
    """Move a note to the trash.

    Args:
        note_id: The ID of the note to trash.
    """
    keep = _get_keep()
    note = keep.get(note_id)
    if note is None:
        raise ValueError(f"Note not found: {note_id}")

    note.trash()
    _sync()
    return f"Note '{note.title}' moved to trash."


@mcp.tool()
def restore_note(note_id: str) -> dict[str, Any]:
    """Restore a note from the trash.

    Args:
        note_id: The ID of the note to restore.
    """
    keep = _get_keep()
    note = keep.get(note_id)
    if note is None:
        raise ValueError(f"Note not found: {note_id}")

    note.untrash()
    _sync()
    return _note_to_dict(note)


@mcp.tool()
def delete_note(note_id: str) -> str:
    """Permanently delete a note. This cannot be undone.

    Args:
        note_id: The ID of the note to permanently delete.
    """
    keep = _get_keep()
    note = keep.get(note_id)
    if note is None:
        raise ValueError(f"Note not found: {note_id}")

    note.delete()
    _sync()
    return f"Note '{note.title}' permanently deleted."


# ---------------------------------------------------------------------------
# List item tools
# ---------------------------------------------------------------------------


@mcp.tool()
def add_list_item(
    note_id: str, text: str, checked: bool = False
) -> dict[str, Any]:
    """Add an item to an existing checklist.

    Args:
        note_id: The ID of the list note.
        text: The item text.
        checked: Whether the item starts checked.
    """
    keep = _get_keep()
    note = keep.get(note_id)
    if note is None:
        raise ValueError(f"Note not found: {note_id}")
    if not isinstance(note, gkeepapi.node.List):
        raise ValueError("Note is not a list/checklist")

    note.add(text, checked)
    _sync()
    return _note_to_dict(note)


@mcp.tool()
def update_list_item(
    note_id: str,
    item_id: str,
    text: str | None = None,
    checked: bool | None = None,
) -> dict[str, Any]:
    """Update a list item's text or checked status.

    Args:
        note_id: The ID of the list note.
        item_id: The ID of the item to update.
        text: New item text (omit to keep current).
        checked: New checked status (omit to keep current).
    """
    keep = _get_keep()
    note = keep.get(note_id)
    if note is None:
        raise ValueError(f"Note not found: {note_id}")
    if not isinstance(note, gkeepapi.node.List):
        raise ValueError("Note is not a list/checklist")

    for item in note.items:
        if item.id == item_id:
            if text is not None:
                item.text = text
            if checked is not None:
                item.checked = checked
            _sync()
            return _note_to_dict(note)

    raise ValueError(f"Item not found: {item_id}")


@mcp.tool()
def delete_list_item(note_id: str, item_id: str) -> dict[str, Any]:
    """Delete an item from a checklist.

    Args:
        note_id: The ID of the list note.
        item_id: The ID of the item to delete.
    """
    keep = _get_keep()
    note = keep.get(note_id)
    if note is None:
        raise ValueError(f"Note not found: {note_id}")
    if not isinstance(note, gkeepapi.node.List):
        raise ValueError("Note is not a list/checklist")

    for item in note.items:
        if item.id == item_id:
            item.delete()
            _sync()
            return _note_to_dict(note)

    raise ValueError(f"Item not found: {item_id}")


# ---------------------------------------------------------------------------
# Label tools
# ---------------------------------------------------------------------------


@mcp.tool()
def list_labels() -> list[dict[str, str]]:
    """List all labels in Google Keep."""
    keep = _get_keep()
    return [{"id": label.id, "name": label.name} for label in keep.labels()]


@mcp.tool()
def create_label(name: str) -> dict[str, str]:
    """Create a new label.

    Args:
        name: The label name.
    """
    keep = _get_keep()
    label = keep.createLabel(name)
    _sync()
    return {"id": label.id, "name": label.name}


@mcp.tool()
def delete_label(name: str) -> str:
    """Delete a label by name. Removes it from all notes.

    Args:
        name: The label name to delete.
    """
    keep = _get_keep()
    label = keep.findLabel(name)
    if label is None:
        raise ValueError(f"Label not found: {name}")

    keep.deleteLabel(label.id)
    _sync()
    return f"Label '{name}' deleted."


@mcp.tool()
def add_label_to_note(note_id: str, label_name: str) -> dict[str, Any]:
    """Add a label to a note. Creates the label if it doesn't exist.

    Args:
        note_id: The ID of the note.
        label_name: The label name to add.
    """
    keep = _get_keep()
    note = keep.get(note_id)
    if note is None:
        raise ValueError(f"Note not found: {note_id}")

    label = keep.findLabel(label_name, create=True)
    note.labels.add(label)
    _sync()
    return _note_to_dict(note)


@mcp.tool()
def remove_label_from_note(note_id: str, label_name: str) -> dict[str, Any]:
    """Remove a label from a note.

    Args:
        note_id: The ID of the note.
        label_name: The label name to remove.
    """
    keep = _get_keep()
    note = keep.get(note_id)
    if note is None:
        raise ValueError(f"Note not found: {note_id}")

    label = keep.findLabel(label_name)
    if label is None:
        raise ValueError(f"Label not found: {label_name}")

    note.labels.remove(label)
    _sync()
    return _note_to_dict(note)


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    """Health check endpoint for Docker/Komodo monitoring."""
    from starlette.responses import JSONResponse

    return JSONResponse({
        "status": "healthy",
        "version": "0.1.0",
        "service": "Google Keep MCP Server",
    })


def main():
    parser = argparse.ArgumentParser(description="Google Keep MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help="Transport mode (default: stdio)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host for HTTP transport (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("PORT", "8080")),
        help="Port for HTTP transport (default: 8080)",
    )
    args = parser.parse_args()

    logger.info("Starting Google Keep MCP server with %s transport", args.transport)

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport=args.transport, host=args.host, port=args.port, stateless_http=True)


if __name__ == "__main__":
    main()
