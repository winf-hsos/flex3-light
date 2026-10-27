"""The pipeline after a recording (docs/pipeline.md): steps that work on a FLEX3 session folder.

Every step reads its inputs from the folder and writes its results into it under the
names of the FLEX3 contract, so FLEX3 Light runs it on the lecturer's computer and the
FLEX3 server can run the same steps later. Nothing here depends on the window.
"""
