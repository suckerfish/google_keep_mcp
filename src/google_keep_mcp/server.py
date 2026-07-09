import argparse
import datetime
import itertools
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


def _blob_to_dict(blob: gkeepapi.node.Blob) -> dict[str, Any]:
    """Serialize an attachment's metadata (no URL — that requires a separate network call)."""
    inner = blob.blob
    result: dict[str, Any] = {"id": blob.id}

    if isinstance(inner, gkeepapi.node.NodeImage):
        result["type"] = "image"
        result["width"] = inner.width
        result["height"] = inner.height
        result["extracted_text"] = inner.extracted_text or ""
    elif isinstance(inner, gkeepapi.node.NodeDrawing):
        result["type"] = "drawing"
        result["extracted_text"] = inner.extracted_text or ""
    elif isinstance(inner, gkeepapi.node.NodeAudio):
        result["type"] = "audio"
        result["length_seconds"] = inner.length
    else:
        result["type"] = "unknown"

    return result


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
        "created": note.timestamps.created.isoformat(),
        "updated": note.timestamps.updated.isoformat(),
        "edited": note.timestamps.edited.isoformat(),
        "attachments": [_blob_to_dict(b) for b in note.blobs],
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


def _parse_date(value: str) -> datetime.datetime:
    """Parse an ISO 8601 date/datetime string, assuming UTC if no timezone given."""
    try:
        dt = datetime.datetime.fromisoformat(value)
    except ValueError as e:
        raise ValueError(f"Invalid date '{value}': {e}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


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


SORT_KEYS = {
    "created": lambda n: n.timestamps.created,
    "updated": lambda n: n.timestamps.updated,
    "edited": lambda n: n.timestamps.edited,
    "title": lambda n: n.title.lower(),
}


@mcp.tool()
def search_notes(
    query: str = "",
    labels: list[str] | None = None,
    color: str | None = None,
    pinned: bool | None = None,
    archived: bool | None = None,
    trashed: bool = False,
    created_after: str | None = None,
    created_before: str | None = None,
    updated_after: str | None = None,
    updated_before: str | None = None,
    sort_by: str | None = None,
    sort_desc: bool = True,
    offset: int = 0,
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
        created_after: ISO 8601 date/datetime; only notes created at or after this.
        created_before: ISO 8601 date/datetime; only notes created at or before this.
        updated_after: ISO 8601 date/datetime; only notes updated at or after this.
        updated_before: ISO 8601 date/datetime; only notes updated at or before this.
        sort_by: Sort by "created", "updated", "edited", or "title". Unsorted
            (arbitrary internal order) if omitted.
        sort_desc: Sort descending (newest/Z-first) when sort_by is set. Default True.
        offset: Number of results to skip (for paging through large result sets).
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

    if created_after is not None:
        dt = _parse_date(created_after)
        results = (n for n in results if n.timestamps.created >= dt)
    if created_before is not None:
        dt = _parse_date(created_before)
        results = (n for n in results if n.timestamps.created <= dt)
    if updated_after is not None:
        dt = _parse_date(updated_after)
        results = (n for n in results if n.timestamps.updated >= dt)
    if updated_before is not None:
        dt = _parse_date(updated_before)
        results = (n for n in results if n.timestamps.updated <= dt)

    if sort_by is not None:
        if sort_by not in SORT_KEYS:
            raise ValueError(
                f"Unknown sort_by '{sort_by}'. Valid options: {', '.join(SORT_KEYS.keys())}"
            )
        results = sorted(results, key=SORT_KEYS[sort_by], reverse=sort_desc)

    return [_note_to_dict(n) for n in itertools.islice(results, offset, offset + limit)]


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
def get_attachment_url(note_id: str, attachment_id: str) -> str:
    """Resolve a fetchable URL for one of a note's attachments (image/drawing/audio).

    Makes a live network call to Google, so only call this for a specific
    attachment you actually need — not in a loop over search results.

    Args:
        note_id: The ID of the note the attachment belongs to.
        attachment_id: The attachment's ID, from the note's "attachments" list.
    """
    keep = _get_keep()
    note = keep.get(note_id)
    if note is None:
        raise ValueError(f"Note not found: {note_id}")

    for blob in note.blobs:
        if blob.id == attachment_id:
            return keep.getMediaLink(blob)

    raise ValueError(f"Attachment not found: {attachment_id}")


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


@mcp.tool()
def bulk_update_notes(
    note_ids: list[str],
    trashed: bool | None = None,
    archived: bool | None = None,
    pinned: bool | None = None,
    add_label: str | None = None,
    remove_label: str | None = None,
) -> list[dict[str, Any]]:
    """Apply the same change(s) to multiple notes in a single sync.

    Does not support permanent deletion — trash notes in bulk here, then use
    delete_note individually once you're sure.

    Args:
        note_ids: IDs of the notes to update.
        trashed: If set, trash (True) or restore (False) all notes.
        archived: If set, archive (True) or unarchive (False) all notes.
        pinned: If set, pin (True) or unpin (False) all notes.
        add_label: Label name to add to all notes (created if it doesn't exist).
        remove_label: Label name to remove from all notes.
    """
    keep = _get_keep()

    notes = []
    for note_id in note_ids:
        note = keep.get(note_id)
        if note is None:
            raise ValueError(f"Note not found: {note_id}")
        notes.append(note)

    add_label_obj = keep.findLabel(add_label, create=True) if add_label else None
    remove_label_obj = None
    if remove_label:
        remove_label_obj = keep.findLabel(remove_label)
        if remove_label_obj is None:
            raise ValueError(f"Label not found: {remove_label}")

    for note in notes:
        if trashed is True:
            note.trash()
        elif trashed is False:
            note.untrash()
        if archived is not None:
            note.archived = archived
        if pinned is not None:
            note.pinned = pinned
        if add_label_obj is not None:
            note.labels.add(add_label_obj)
        if remove_label_obj is not None:
            note.labels.remove(remove_label_obj)

    _sync()
    return [_note_to_dict(n) for n in notes]


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
