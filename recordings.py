"""What a lecturer can do with their recordings, for the page "Meine Aufnahmen".

The app and the server share it (decision of 2026-11-06, phase 4): the app on the
recordings in its archive, the server on the session folders of whoever asks. Both
mix Recordings into a class that provides:

  _library            session id -> folder of the recordings shown (set by recordings())
  pipeline            the PipelineRunner that works on them
  cut_settings()      the lecturer's cut settings, set_cut_settings(values) changes them
  _scan()             session id -> folder of the lecturer's recordings
  _find_module(name)  the lecturer's module of a recording's course_name, or None
  _add_term(m, term)  add a technical term to that module
  _discard(folder)    remove a recording (the app: recycle bin, the server: its trash)

Every action returns an error message for the window, or "" when it went through.
"""
from __future__ import annotations

import json
from pathlib import Path

import library
from pipeline import cut_rules, edit, locks, publish, review
from pipeline import style as styles


class Recordings:
    def recordings(self) -> list[dict]:
        """All session folders in the archive, newest first, with their way through the pipeline."""
        self._library = self._scan()
        items = [library.summary(f, self.pipeline.queued(f)) for f in self._library.values()]
        return sorted(items, key=lambda r: r["recorded_at"], reverse=True)

    def recording(self, sid: str) -> dict | None:
        folder = getattr(self, "_library", {}).get(sid)
        if folder is None:
            return None
        view = {**library.detail(folder, self.pipeline.queued(folder)), "status": self.pipeline.describe(folder)}
        made = (view.get("cut") or {}).get("render")
        if made:                                   # rendered in the style the server has now?
            now = styles.load(getattr(self.pipeline, "style_dir", None))
            made["style_current"] = (made.get("style") or styles.fingerprint(styles.load(None))) == styles.fingerprint(now)
            made["style_now"] = now["name"]
        return view

    def start_transcription(self, sid: str) -> str:
        """Transcribe a recording by hand that was only filed; returns an error or ''."""
        folder = getattr(self, "_library", {}).get(sid)
        if folder is None or not folder.exists():
            return "Die Aufnahme ist nicht mehr in der Ablage."
        self.pipeline.enqueue(folder)
        return ""

    def _edit_of(self, sid: str):
        """(folder, edit list) of a recording the lecturer may change now, or an error message."""
        folder = getattr(self, "_library", {}).get(sid)
        if folder is None or not folder.exists():
            return None, "Die Aufnahme ist nicht mehr in der Ablage."
        if self.pipeline.active == str(folder):
            return None, "Der Schnitt-Agent oder die Transkription arbeitet gerade an dieser Aufnahme."
        e = edit.load(folder)
        if e is None:
            return None, "Für diese Aufnahme gibt es noch keinen Schnitt-Vorschlag."
        return (folder, e), ""

    def _save_edit(self, folder: Path, e: dict) -> None:
        e.pop("approved", None)                    # every change asks for a new approval
        edit.save(folder, e)

    def _words(self, folder: Path) -> list[dict]:
        return json.loads((folder / "transcript.json").read_text(encoding="utf-8")).get("words", []) \
            if (folder / "transcript.json").exists() else []

    def change_cut_item(self, sid: str, item_id: str, changes: dict) -> str:
        """Accept, reject, move the bounds or change the text of one item; returns an error or ''."""
        found, error = self._edit_of(sid)
        if error:
            return error
        folder, e = found
        x = edit.find(e, item_id)
        if x is None:
            return "Diesen Vorschlag gibt es nicht mehr."
        words = self._words(folder)
        for k in ("start", "end"):
            if changes.get(k) is not None and not (k == "end" and x["end"] is None):
                t = min(e["duration"], max(0.0, float(changes[k])))
                x[k] = edit.snap(t, words) if x["kind"] not in edit.SPANLESS else round(t, 3)
        if x["end"] is not None and x["end"] <= x["start"]:
            return "Das Ende muss nach dem Anfang liegen."
        if changes.get("state") in edit.STATES:
            x["state"] = changes["state"]
        for k, v in (changes.get("params") or {}).items():
            x["params"][k] = v
        x["by_lecturer"] = True
        e["items"].sort(key=lambda i: (i["start"], i["kind"]))
        self._save_edit(folder, e)
        return ""

    def add_cut_item(self, sid: str, kind: str, start: float, end: float | None, params: dict | None = None) -> str:
        found, error = self._edit_of(sid)
        if error:
            return error
        folder, e = found
        words = self._words(folder)
        try:
            if kind not in edit.SPANLESS:
                start, end = edit.snap(float(start), words), edit.snap(float(end), words)
            x = edit.item(kind, float(start), end, "Von dir gesetzt", "lecturer", 1.0, "accepted", **(params or {}))
        except (TypeError, ValueError) as ex:
            return str(ex)
        edit.add(e, x)
        self._save_edit(folder, e)
        return ""

    def cover_lock(self, sid: str, lock_id: str) -> str:
        """Hide a privacy lock: a mute, a blur or a cut over its span, set as the lecturer's own."""
        found, error = self._edit_of(sid)
        if error:
            return error
        folder, e = found
        lock = next((x for x in locks.load(folder).get("locks", []) if x["id"] == lock_id), None)
        if lock is None:
            return "Diese Sperre gibt es nicht mehr."
        edit.add(e, locks.cover_item(lock, self._words(folder)))
        e.get("lifted", {}).pop(lock_id, None)
        self._save_edit(folder, e)
        return ""

    def lift_lock(self, sid: str, lock_id: str, lifted: bool = True) -> str:
        """Let a lock pass: only the lecturer, only by hand, for one lock at a time (decision of 2026-11-10)."""
        found, error = self._edit_of(sid)
        if error:
            return error
        folder, e = found
        if not any(x["id"] == lock_id for x in locks.load(folder).get("locks", [])):
            return "Diese Sperre gibt es nicht mehr."
        if lifted:
            e.setdefault("lifted", {})[lock_id] = {"at": edit._now()}
        else:
            e.get("lifted", {}).pop(lock_id, None)
        self._save_edit(folder, e)
        return ""

    def remove_cut_item(self, sid: str, item_id: str) -> str:
        found, error = self._edit_of(sid)
        if error:
            return error
        folder, e = found
        x = edit.find(e, item_id)
        if x is None or x["source"] != "lecturer":
            return "Nur selbst gesetzte Einträge lassen sich entfernen; Vorschläge lehnst du ab."
        e["items"].remove(x)
        self._save_edit(folder, e)
        return ""

    def set_cut_metadata(self, sid: str, title: str, description: str) -> str:
        found, error = self._edit_of(sid)
        if error:
            return error
        folder, e = found
        e["title"], e["description"] = title.strip(), description.strip()
        self._save_edit(folder, e)
        return ""

    def set_program_rule(self, sid: str, program: str, choice: str) -> str:
        """choice "cut" or "keep" remembers the program for all recordings, "" forgets it; the program's
        spans in this recording are proposed again accordingly."""
        program = program.strip().lower()
        policy = self.cut_settings()
        cut = set(policy["cut_programs"]) - {program}
        keep = set(policy["keep_programs"]) - {program}
        (cut if choice == "cut" else keep if choice == "keep" else set()).add(program)
        self.set_cut_settings({"cut_programs": sorted(cut), "keep_programs": sorted(keep)})
        found, error = self._edit_of(sid)
        if error:
            return "" if "noch keinen" in error else error
        folder, e = found
        e["items"] = [x for x in e["items"] if not (x["params"].get("rule") == "program" and not x.get("by_lecturer"))]
        log = json.loads((folder / "activity.json").read_text(encoding="utf-8")) if (folder / "activity.json").exists() else {}
        for x in cut_rules.programs(log.get("activity", []), self.cut_settings()):
            edit.add(e, x)
        self._save_edit(folder, e)
        return ""

    def set_cut_cards(self, sid: str, cards: dict) -> str:
        """The lecturer's texts and switches of the title, chapter and closing cards."""
        found, error = self._edit_of(sid)
        if error:
            return error
        folder, e = found
        meta = json.loads((folder / "session.json").read_text(encoding="utf-8"))
        e["cards"] = edit.clean_cards(cards or {}, e.get("cards") or edit.default_cards(e, meta, self.cut_settings()))
        self._save_edit(folder, e)
        return ""

    def approve_cut(self, sid: str, approved: bool = True) -> str:
        found, error = self._edit_of(sid)
        if error:
            return error
        folder, e = found
        if approved:
            error = edit.approve(folder, self.cut_settings())
            if error:
                return error
            self.pipeline.failed.discard(str(folder))  # a failed render is tried again
            self.pipeline.enqueue(folder)            # render it
        else:
            self._save_edit(folder, e)
        return ""

    def redo_cut(self, sid: str, version: int | None = None) -> str:
        """Propose the cut (again); version 1 or 2 of the agent for this proposal, None: the server's choice."""
        folder = getattr(self, "_library", {}).get(sid)
        if folder is None or not folder.exists():
            return "Die Aufnahme ist nicht mehr in der Ablage."
        if library.stations(folder)["transcript"] != "done":
            return "Der Schnitt braucht zuerst die Transkription."
        if not self.pipeline.redo_cut(folder, version if version in (1, 2) else None):
            return "An dieser Aufnahme wird gerade gearbeitet."
        return ""

    def recheck(self, sid: str) -> str:
        """Check the cut on its rendered result again (after changes at the approval); returns an error or ''."""
        folder = getattr(self, "_library", {}).get(sid)
        if folder is None or not folder.exists():
            return "Die Aufnahme ist nicht mehr in der Ablage."
        if not (folder / "edit.json").exists():
            return "Es gibt noch keinen Schnitt zum Prüfen."
        if not self.pipeline.redo_check(folder):
            return "An dieser Aufnahme wird gerade gearbeitet."
        return ""

    def rerender(self, sid: str) -> str:
        """Render the approved cut again, e.g. after its videos were deleted; returns an error or ''."""
        folder = getattr(self, "_library", {}).get(sid)
        if folder is None or not folder.exists():
            return "Die Aufnahme ist nicht mehr in der Ablage."
        e = edit.load(folder)
        if not (e and e.get("approved")):
            return "Neu rendern geht nur mit einem freigegebenen Schnitt."
        if not self.pipeline.redo_render(folder):
            return "An dieser Aufnahme wird gerade gearbeitet."
        return ""

    def make_summary(self, sid: str) -> str:
        """Write the summary of the rendered video (again); returns an error or ''."""
        folder = getattr(self, "_library", {}).get(sid)
        if folder is None or not folder.exists():
            return "Die Aufnahme ist nicht mehr in der Ablage."
        if not (folder / "render.json").exists():
            return "Die Zusammenfassung braucht zuerst das geschnittene Video."
        if not self.pipeline.redo_summary(folder):
            return "An dieser Aufnahme wird gerade gearbeitet."
        return ""

    def redo_transcription(self, sid: str) -> str:
        """Queue a recording's transcription again, with the module's current terms; returns an error or ''."""
        folder = getattr(self, "_library", {}).get(sid)
        if folder is None or not folder.exists():
            return "Die Aufnahme ist nicht mehr in der Ablage."
        if not self.pipeline.redo(folder):
            return "Die Aufnahme wird gerade transkribiert."
        return ""

    def review_word(self, sid: str, index: int, text: str, as_term: bool = False) -> dict:
        """Correct or confirm an unsure word of a transcript, optionally adding it to the module's terms.

        Returns {"ok", "message"} for the window.
        """
        folder = getattr(self, "_library", {}).get(sid)
        if folder is None or not folder.exists():
            return {"ok": False, "message": "Die Aufnahme ist nicht mehr in der Ablage."}
        if library.stations(folder)["transcript"] != "done":
            return {"ok": False, "message": "Die Transkription läuft gerade, danach geht es wieder."}
        try:
            old = json.loads((folder / "transcript.json").read_text(encoding="utf-8"))["words"][int(index)]["word"]
            word = (review.confirm(folder, int(index)) if text.strip() == old.strip()
                    else review.correct(folder, int(index), text))
        except (ValueError, IndexError, OSError) as e:
            return {"ok": False, "message": str(e) or "Das Wort ließ sich nicht ändern."}
        if not as_term:
            return {"ok": True, "message": ""}
        name = json.loads((folder / "session.json").read_text(encoding="utf-8")).get("course_name", "")
        module, new = self._find_module(name), review.term(word["word"])
        if module is None:
            return {"ok": True, "message": f"Korrigiert, aber das Modul „{name}“ fehlt in deinen Modulen."}
        if new and new not in module.get("terms", []):
            self._add_term(module, new)
        return {"ok": True, "message": f"„{new}“ steht jetzt in den Fachbegriffen von {module['name']}."}

    def delete_recording(self, sid: str) -> str:
        """Move a recording's session folder with all its files to the recycle bin; returns an error or ''."""
        folder = getattr(self, "_library", {}).get(sid)
        if folder is None or not folder.exists():
            return "Die Aufnahme ist nicht mehr in der Ablage."
        if not self.pipeline.forget(folder):
            return "Die Aufnahme wird gerade transkribiert. Lösch sie, wenn das fertig ist."
        try:
            self._discard(folder)
        except OSError as e:
            return f"Löschen fehlgeschlagen: {e}"
        self._library.pop(sid, None)
        return ""

    def _media(self, sid: str, name: str) -> Path | None:
        """For the media server: a video or subtitle file of a listed recording, nothing else."""
        folder = getattr(self, "_library", {}).get(sid)
        return publish.locate(folder, name) if folder and library.MEDIA.match(name) else None
