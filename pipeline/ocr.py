"""Text in a picture with its position, from the OCR built into Windows (Windows.Media.Ocr).

Local, fast and free: no model sees the picture for this. The engine is reached through
Windows PowerShell, which can call Windows Runtime APIs directly, so no extra Python
package is needed. Each call starts PowerShell (about a second), which is fine for the
few pictures the cut agent asks about.

recognize() returns the lines with their words and pixel boxes; find() turns a search
into regions as fractions of the picture, the form the edit list uses.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from . import binaries

MAX_SIDE = 2560                      # Windows' OCR refuses larger pictures (OcrEngine.MaxImageDimension)

SCRIPT = r'''
param([string]$Path, [string]$Lang = "de-DE", [string]$List = "")
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType = WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, [Type]$type) {
    $task = $asTask.MakeGenericMethod($type).Invoke($null, @($op)); $task.Wait(-1) | Out-Null; $task.Result }
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new($Lang))
if (-not $engine) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }
function Read-Picture([string]$P) {
    $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($P)) ([Windows.Storage.StorageFile])
    $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    $lines = @()
    foreach ($line in $result.Lines) {
        $words = @()
        foreach ($w in $line.Words) {
            $r = $w.BoundingRect
            $words += @{ text = $w.Text; x = $r.X; y = $r.Y; w = $r.Width; h = $r.Height } }
        $lines += @{ text = $line.Text; words = $words } }
    $stream.Dispose()
    @{ width = $bitmap.PixelWidth; height = $bitmap.PixelHeight; lines = $lines } | ConvertTo-Json -Depth 5 -Compress
}
if ($List) {            # many pictures with one start of PowerShell: one line of JSON per picture
    foreach ($p in (Get-Content -LiteralPath $List -Encoding UTF8)) {
        if (-not $p) { continue }
        try { Read-Picture $p } catch { '{"error": "unreadable"}' } }
} else { Read-Picture $Path }
'''

_script_file: Path | None = None


def _script() -> Path:
    global _script_file
    if _script_file is None or not _script_file.exists():
        _script_file = Path(tempfile.gettempdir()) / "flex3-light-ocr.ps1"
        _script_file.write_text(SCRIPT, encoding="utf-8-sig")       # Windows PowerShell 5.1 wants the BOM
    return _script_file


def recognize(image: Path, language: str = "de") -> dict:
    """{"width", "height", "lines": [{"text", "words": [{"text", "x", "y", "w", "h"}]}]} in pixels."""
    if os.name != "nt":           # a server on Linux: the agent gets this as the tool's answer and goes on
        raise RuntimeError("Texterkennung gibt es nur unter Windows; auf diesem Server nicht verfügbar")
    tag = {"de": "de-DE", "en": "en-US"}.get(language, language)
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                        "-File", str(_script()), "-Path", str(Path(image).resolve()), "-Lang", tag],
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=60, creationflags=binaries.flags())
    out = r.stdout.decode("utf-8", "replace").strip()
    if r.returncode != 0 or not out.startswith("{"):
        raise RuntimeError("Texterkennung fehlgeschlagen: " + (r.stderr.decode("utf-8", "replace") or out)[-300:])
    return _tidy(json.loads(out))


def recognize_many(images: list[Path], language: str = "de", timeout: float = 3600) -> list[dict | None]:
    """recognize() for many pictures with one start of PowerShell; None for a picture it could not read."""
    if os.name != "nt":
        raise RuntimeError("Texterkennung gibt es nur unter Windows; auf diesem Server nicht verfügbar")
    if not images:
        return []
    tag = {"de": "de-DE", "en": "en-US"}.get(language, language)
    handle, name = tempfile.mkstemp(prefix="flex3-ocr-", suffix=".txt")
    os.close(handle)
    listing = Path(name)
    try:
        listing.write_text("\n".join(str(Path(p).resolve()) for p in images), encoding="utf-8-sig")
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                            "-File", str(_script()), "-List", str(listing), "-Lang", tag],
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                           creationflags=binaries.flags(low=True))
    finally:
        listing.unlink(missing_ok=True)
    rows = [x for x in r.stdout.decode("utf-8", "replace").splitlines() if x.strip().startswith("{")]
    if r.returncode != 0 or len(rows) != len(images):
        raise RuntimeError("Texterkennung fehlgeschlagen: " + r.stderr.decode("utf-8", "replace")[-300:])
    out = []
    for row in rows:
        data = json.loads(row)
        out.append(None if "error" in data else _tidy(data))
    return out


def _tidy(data: dict) -> dict:
    lines = data.get("lines") or []
    if isinstance(lines, dict):                                     # PowerShell drops the list around a single line
        lines = [lines]
    for line in lines:
        if isinstance(line.get("words"), dict):
            line["words"] = [line["words"]]
    data["lines"] = lines
    return data


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def find(result: dict, query: str, pad: float = 0.006) -> list[dict]:
    """Regions of the words that make up the query, as fractions [x, y, w, h] of the picture, padded a little.

    An empty query returns every line, for scanning a picture for anything personal."""
    width, height = float(result["width"]), float(result["height"])
    found = []
    q = _norm(query)
    for line in result["lines"]:
        words = line.get("words") or []
        if not words:
            continue
        if not q:
            hits = [words]
        else:
            hits = []
            tokens = [_norm(w["text"]) for w in words]
            for i in range(len(words)):                            # the shortest run of words containing the query
                text = ""
                for j in range(i, len(words)):
                    text = (text + " " + tokens[j]).strip()
                    if q in text or q.replace(" ", "") in text.replace(" ", ""):
                        hits.append(words[i:j + 1])
                        break
                    if len(text) > len(q) + 40:
                        break
            hits = [h for h in hits if not any(h is not o and set(map(id, o)) < set(map(id, h)) for o in hits)]
        for run in hits:
            x0 = min(w["x"] for w in run); y0 = min(w["y"] for w in run)
            x1 = max(w["x"] + w["w"] for w in run); y1 = max(w["y"] + w["h"] for w in run)
            px, py = pad, pad * width / height
            region = [max(0.0, x0 / width - px), max(0.0, y0 / height - py),
                      min(1.0, (x1 - x0) / width + 2 * px), min(1.0, (y1 - y0) / height + 2 * py)]
            found.append({"text": " ".join(w["text"] for w in run), "line": line["text"],
                          "region": [round(v, 4) for v in region]})
    return found[:60]
