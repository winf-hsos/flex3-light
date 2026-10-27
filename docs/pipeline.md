# From recording to finished videos: the pipeline

Status: concept of 2026-10-26, nothing of it is built yet. Decisions by Nicolas (project lead) are marked with the date; open points are listed at the end.

FLEX3 Light does on the lecturer's own computer what the FLEX3 server will do later: after the recording it transcribes, proposes a cut, renders a long version and short clips, writes subtitles and AI content, and prepares everything for YouTube and Opencast. The steps are built once and reused by the server (decision of 2026-10-26), so there is one pipeline with two places to run, not two pipelines.

## Guiding principles (2026-10-26)

1. **Intuitive, step by step.** Every recording moves through a few visible steps, and the app only stops where the lecturer has to decide something.
2. **Simple to configure.** Everything works with defaults; API keys are optional and a missing one explains itself instead of failing.
3. **As little load as possible during the lecture.** While recording, only what the recording needs runs. Everything heavy waits until the lecture is over.

## What the lecturer sees

**Before and after recording, the app may use the whole screen** (2026-10-26). The start view lists the recordings as cards, newest first, with a button "Neue Aufnahme". **While recording, only the small control bar is visible**, as today.

Each recording card shows its steps, the current one highlighted, with one button that leads on:

| Step | Runs | The lecturer |
|---|---|---|
| **Aufgenommen** | automatically at the stop | – |
| **Transkribiert** | automatically, in the cloud | – |
| **Schnitt prüfen** | the agent proposes, ffmpeg renders after approval | looks at the proposals on a timeline, changes or accepts them |
| **Inhalte prüfen** | summary, quiz, simple language, subtitles in more languages | reads, corrects, approves |
| **Bereit** | YouTube and Opencast packages written; Opencast upload once automated | copies title and description, uploads the video |

A first-start assistant asks only for screen, camera, microphone and the first module. API keys for OpenAI and ElevenLabs are entered in the settings and kept in the Windows Credential Manager. A step whose key is missing shows one line of explanation and a button "Schlüssel hinterlegen".

## The session folder is the unit of work

The pipeline works on the session folder of the FLEX3 contract (`session-folder.md` in the FLEX3 repository) and writes its results there under the names the contract already gives: `transcript.json`, `subtitles.<language>.vtt`, `chapters.json`, `summary.<language>.md` and the like. That is what makes it reusable on the server: the server calls the same steps on the same folders.

FLEX3 Light therefore writes a complete session folder locally as well, one folder per recording in the archive (decision of 2026-10-26): `<archive>\<term>\<module>\<date> - <topic>\` with `recording.mp4` (program mix), `screen.mp4`, `camera.mp4`, `session.json` and, as they come, all results of the pipeline. Until then it filed the two tracks directly in the module folder and kept the metadata in a manifest that was deleted after filing; recordings filed that way stay as they are.

Each step records its state in `pipeline.json` in the folder: done or not, with which inputs and settings. A step runs again only when its inputs changed, so the pipeline can be stopped and resumed at any point, after a crash as well as after a correction by the lecturer.

## The steps

### 1. Recording (built, together with the session folder, on 2026-10-26)

Screen, camera and microphone through OBS, a new file every minute, filed as a session folder. Two cheap additions during the recording, because they make the cut much easier and need no AI (`activity.json` in the session folder, kept local because window titles can hold personal data):

- **Activity log.** Every quarter second FLEX3 Light notes which program is in the foreground (process name and window title), as it already notes Alt+Tab. Afterwards it is known that Outlook was visible from 12:03 to 12:20, without sending a single frame anywhere.
- **Privacy key.** Ctrl+Alt+H or the eye button in the recording bar freezes the screen track at once until it is pressed again, with the same freezing that hides Alt+Tab. Preventing a sensitive picture is more reliable than finding it afterwards.

**Why one-minute segments** (the question of 2026-10-26): they let FLEX3 Light process and upload the recording while it is still running, so that a long lecture is filed and on the server seconds after the stop instead of minutes later. They also limit what a crash can cost. OBS writes them at no extra cost; only the processing of each segment costs time. Under principle 3 that processing becomes adaptive: with NVENC and a calm processor it runs as today, on a weak laptop or on battery it waits until after the stop.

### 2. Transcription (cloud, decision of 2026-10-26)

**Built on 2026-10-26** in `pipeline/` (audio pieces, `whisper-1` adapter, prompt with the module's terms, invented-text check, punctuation, subtitles) and `pipeline_runner.py` (queue after filing, only while not recording, waits for the key). Tested on the German test session: 5 minutes in 23 seconds, 963 words with timestamps, 92 subtitle cues. Still open from this section: the correction by a language model and the check for quiet voices from the room.

**AssemblyAI since 2026-10-27** (decision of the project lead after the comparison below): `transcribe.AssemblyAI` sends the whole recording as one MP3 to Universal-3 Pro, in the region that accepts the key (EU first), with the module's terms as key terms, takes words and sentences as they come and deletes the transcript at AssemblyAI right after fetching it. `whisper-1` remains the fallback in the runner until 2027-02-26. On the German test session through the real runner: 16 seconds, 966 words, 50 sentences, 93 subtitle cues.

The project's video assistant has transcribed about ten lectures this way already (OpenAI `whisper-1`, German and English); its procedure and experience, reported on 2026-10-26, shape this step. Its code can be read under `06 - Kommunikation\03 - Videos\.claude\skills\videoschnitt\werkzeuge\` (`transcribe`, corrections in `vp.py`, editing in `lektorat.py`) and is copied, not changed, where we take something over.

- **Audio:** mono 16 kHz from the recording, cut at speech pauses (ffmpeg `silencedetect`, −35 dB, 0.4 s) into pieces of about ten minutes and sent as MP3 at 48 kbit/s, which keeps each piece well under OpenAI's 25 MB. Word times are shifted back onto the common timeline, finished pieces are cached. Each piece gets context and the module's word list as prompt (with `whisper-1` about 800 characters fit the 224-token limit).
- **Model and word timestamps.** Word timestamps come directly from the model only with `whisper-1`, which is deprecated and will be shut down on 2027-02-26; the recommended `gpt-transcribe` documents `prompt`, `keywords` and `languages` but no word timestamps ([OpenAI speech-to-text guide](https://developers.openai.com/api/docs/guides/speech-to-text)). The video assistant got a 403 for `gpt-4o-transcribe` with the project key on 2026-10-22. So the transcription sits behind an adapter, and two successors are compared on the two released test sessions of the FLEX3 repository, one German and one English, before `whisper-1` ends:
  1. **ElevenLabs Scribe,** which according to ElevenLabs returns word timestamps, speaker labels and non-speech events directly and states a German word error rate of 3.1 % on FLEURS (vendor figure; whether the API gives word or character timestamps is to be checked in the API reference). It would need no extra alignment, and ElevenLabs is planned anyway for music and dubbing, so one key would serve three steps.
  2. **`gpt-transcribe` plus a local forced alignment** (the method WhisperX uses), which gives every word its time after the lecture, on the CPU.
  3. **AssemblyAI** (added 2026-10-27; the project lead already uses it for research interviews), with word timestamps from the model and an EU region (`api.eu.assemblyai.com`), where the project's key lives.

  **First comparison, 2026-10-27,** on the German test session (5 minutes, same 48 kbit/s MP3 and the same three terms as for `whisper-1`; Universal-2 takes no terms for German). There is no reference transcript, so the table counts where the models disagree and judges the clear cases from the context:

  | | `whisper-1` | AssemblyAI Universal-3 Pro | Universal-3.5 Pro | Universal-2 |
  |---|---|---|---|---|
  | time for 5 min | 23 s | 11 s | 16 s | 7 s |
  | words | 963 | 967 | 960 | 948 |
  | "eine KI auf unserem Rechner" | "Klinik" | right | right | – |
  | "mit er oder es, meine ich den Agenten" | "R oder S" | right | right | "R oder S" |
  | "Exposé" (a given term) | "Explosion" | right | right | – |
  | "das geht dich gar nichts an" | right | "sich" | word missing | – |
  | restarts and fillers ("Genau, die ich hier mal aufliste") | often left out | kept | partly | partly |
  | price per hour | about 0.36 USD | 0.21 USD (+0.05 for terms) | 0.21 USD | 0.15 USD |

  Word times of Universal-3 Pro and 3.5 Pro agree within 20 ms (95 %), while `whisper-1` differs from both by 30 ms in the median and up to 310 ms; the two AssemblyAI models are probably closer to the truth, which only listening can confirm. Universal-2 misspells "Quarto" and takes no terms in German, so it is out. Prices from [AssemblyAI's pricing](https://www.assemblyai.com/pricing) as of 2026-10-27.
  Measured are errors in technical terms and the accuracy of the word times.
- **Hallucinations.** In silence Whisper writes "Thank you", "Thanks for watching" or repeats the prompt. Such pieces are recognised and sent again without prompt, and words that fall into silence are dropped.
- **Correction:** without a word list the model mishears technical terms ("Chat TPT", "Cloud Code" for Claude Code, "Live File" for LiFi, "2 4" for 2.4). The module's word list from the profile goes into the prompt, and afterwards a language model goes over the whole transcript once and fixes the rest without changing the timing, as general replacements plus corrections at single positions (for a three-hour session the video assistant needed about 100 and 40).
- **Result:** `transcript.json` with word timestamps, the central artifact everything else is derived from; the raw transcript is kept as `transcript.raw.json`.
- **Cost:** with `whisper-1` about 0.006 USD per minute in the video assistant's experience, so about 1 USD for three hours. Prices of the successors are still to be checked.

### 3. Cut, proposed by an agent and approved by the lecturer

The agent does not cut videos. It writes an **edit list** (`edit.json`): spans to remove, spans to freeze or blur, chapter marks with titles, title and closing sequence, music and sound effects at chapter changes. ffmpeg renders from that list, deterministically and repeatably. Nothing is rendered before the lecturer has approved it on a simple timeline.

**Voices from the room are the biggest privacy risk, and the transcript hides them.** Questions from students are often about 30 dB quieter than the lecturer. Whisper then leaves them out or writes "Thank you", but they remain audible in the video. The video assistant therefore transcribes every pause of 1.5 seconds or more again on its own, after loudness normalisation, and blocks what has content: on 2026-10-22 that was 20 spans, one of them a person's own CV. The cut agent does the same and proposes muting or cutting these spans; approving the cut means looking at them.

Its inputs, from cheap and private to expensive:

1. the transcript (chapters, false starts, long pauses, "this is not for the recording"),
2. the activity log and the privacy-hotkey marks (Explorer, Outlook, browser with personal tabs),
3. only for spans the first two flag: a few frames, analysed locally first; sending frames to a cloud vision model is a setting the lecturer has to switch on, because those are exactly the frames that may show personal data.

**Music** comes from a small library of pieces generated beforehand with ElevenLabs (2026-10-26), for the title, chapter changes and the closing sequence rather than under speech. Sound effects at chapter changes come from the same library.

### 4. Subtitles

WebVTT from the corrected transcript, cut at sentence and pause boundaries (`subtitles.de.vtt`). Translations by a language model, cue by cue, keeping the times (`subtitles.en.vtt` and so on). After a cut, the subtitles follow the edit list.

### 5. Finished videos

- **Long version** of the whole lecture, rendered from the edit list.
- **Short clips** per chapter, for topics that stand on their own.
- **YouTube: prepared, not uploaded** (2026-10-26). A folder per video with the video file, the title, and the description with chapter marks to copy. YouTube's rules for chapters apply: the first timestamp is 00:00, at least three chapters in ascending order, each at least 10 seconds long ([YouTube help](https://support.google.com/youtube/answer/9884579)).
- **Opencast: prepared, and automated where possible** (2026-10-26) through Opencast's API into the module's series. That needs access from the computing centre.

### 6. AI content

Summary, a version in simple language, topic overview and quiz from `transcript.json`, with prompts kept as files in the repository so that they can be improved without programming. Everything is marked as AI-generated and approved by the lecturer before anyone else sees it.

### 7. Dubbing

Only with the lecturer's own ElevenLabs key (2026-10-26, because of the cost), and a cloned voice only after the lecturer has agreed explicitly in the settings, in line with `consent.voice_clone` in `session.json`.

### 8. Live subtitles (experiment)

Streaming speech recognition during the lecture, shown in a transparent subtitle line over the screen. With the window exclusion that already hides the control bar, the line is visible on the projector but does not end up in the recording, which keeps its own, better subtitles from step 4. This is variant C2 of the FLEX3 live concept. Because it runs during the lecture, it is the one step that works against principle 3; it is switched off by default.

## Where the work runs

| | during the lecture | after the lecture |
|---|---|---|
| OBS recording, one-minute segments | yes | – |
| activity log, privacy hotkey | yes, negligible | – |
| splitting segments into tracks | only with NVENC and a calm processor | otherwise here |
| transcription | – | cloud |
| alignment, rendering, frames | – | locally, low priority, waits for mains power |
| language model steps, dubbing | – | cloud |

## How it is built

- A package `pipeline/` in this repository, independent of the window: each step is a function `run(session_dir, settings)` that reads its inputs from the folder and writes its outputs there. The app calls it after the stop; the server will call the same functions.
- Providers behind small adapters, as the FLEX3 transcription design asks: transcription (OpenAI now, local Whisper or another provider later), language model, ElevenLabs.
- Prompts and the edit list format as files, versioned in the repository.
- Keys in the Windows Credential Manager, never in `settings.json`, never in logs.

## Order of work

1. Local session folder with `session.json`, the activity log and the privacy key. Useful at once, and the base for everything else. Done on 2026-10-26; the program mix is now made for every recording, which adds some load per segment during the lecture until the adaptive processing of principle 3 is built.
2. Transcription with alignment, word-list correction and subtitles.
3. Chapters, short clips, YouTube and Opencast packages.
4. AI content.
5. Cut agent with the timeline for approval, music library.
6. Opencast automation, dubbing, live subtitles as an experiment.

## Open points

- **Successor of `whisper-1`:** AssemblyAI Universal-3 Pro is built in (2026-10-27). Still to do: the English test session, a listening check of the disputed spots, and whether Universal-3.5 Pro is the better default once it has been compared on more material. For it, check in the API references (OpenAI's was not reachable on 2026-10-26) whether `gpt-transcribe` returns any timestamps and whether Scribe's are per word or per character, and why the project key got a 403 for `gpt-4o-transcribe`.
- **Prices** for the successors and the language model per lecture hour; OpenAI's pricing page was not reachable either.
- **Opencast access** from the computing centre for the automated upload.
- **Rights of the ElevenLabs music** for publication on YouTube, depending on the plan.
- **Cloud processing and data protection:** the transcription sends the lecture's audio to OpenAI. The FLEX3 decisions allow cloud models when they are clearly better and ask for this to be disclosed; frames go to a cloud model only when the lecturer switches it on.
