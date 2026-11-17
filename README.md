# ComfyUI-CivitFetch

A ComfyUI extension that lets you paste a **[CivitAI](https://civitai.com/)** or
**[Civitai Red](https://civitai.red/)** model link and download the model straight into
the correct `models/` subfolder — chosen from a dropdown of your existing folders.

It adds a **CivitFetch** panel to ComfyUI's sidebar with:

1. A text box to paste the model link.
2. A dropdown listing every folder under your ComfyUI `models/` directory.

Click **Download** and the model streams into the selected folder with a live progress bar.

## Install

Search for **CivitFetch** in ComfyUI-Manager, or clone into your ComfyUI `custom_nodes` directory:

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/retrodiffuse/ComfyUI-CivitFetch
```

`requests` is already bundled with ComfyUI; if you run a minimal install:

```bash
pip install -r ComfyUI-CivitFetch/requirements.txt
```

Restart ComfyUI. A download icon labelled **CivitFetch** appears in the left sidebar.

## API key (for gated models)

Many CivitAI downloads (login-required or early-access models) need an API key.

1. On civitai.com go to **Account Settings → API Keys** and create one.
2. In ComfyUI open **Settings** (gear icon) → search **CivitFetch** → paste the key into
   **CivitAI API Key**.

The key is stored in your ComfyUI settings and appended to downloads automatically. Public
models download fine without one.

The key never lives in this repo or the custom node folder, and CivitFetch masks it
(`token=***`) in console output and error messages, so logs are safe to paste into an issue.

## Usage

1. Open the **CivitFetch** sidebar tab.
2. Paste a link. Both of these work (and their `civitai.red` equivalents):
   - A model page: `https://civitai.com/models/1234?modelVersionId=5678`
   - A direct download link: `https://civitai.com/api/download/models/5678`
3. Pick the destination folder (e.g. `checkpoints`, `loras`, `loras/character`).
4. Click **Download**.

Use the **⟳** button to refresh the folder list if you add folders while ComfyUI is running.

## Notes

- Downloads write to a `.part` file and are renamed on completion, so an interrupted
  download won't leave a broken model in place.
- Filename collisions are auto-resolved (` (1)`, ` (2)`, …).
- The destination is validated to stay inside your `models/` directory.

## License

MIT
