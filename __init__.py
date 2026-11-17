"""ComfyUI-CivitFetch — download CivitAI / Civitai Red models into ComfyUI's models/ folders.

This package registers HTTP routes on ComfyUI's server (see civitfetch.api) and serves the
frontend sidebar panel from ./web via WEB_DIRECTORY. It intentionally exposes no graph nodes.
"""

# Serve everything in ./web as static frontend assets (ComfyUI auto-loads *.js from here).
WEB_DIRECTORY = "./web"

# No graph nodes — this extension is a sidebar panel + backend routes only.
NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}

# Importing the api module registers the aiohttp routes on PromptServer at load time.
from .civitfetch import api  # noqa: E402,F401

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
