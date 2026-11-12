#!/usr/bin/env python3
# Eclipse Proof MCP
# Copyright © 2026 Cyan Eclipse Technologies LLC · cetech.ai
"""Eclipse Proof MCP server (stdio, stdlib). Agent surface = 15 tools ONLY.
Operator surface is DISJOINT: proof_operator.py process, Herald key held
there, never here. Unknown tool name -> SURFACE_TOOL_UNKNOWN, byte-identical
shape for ANY unknown name (no oracle re operator surface, spec §5).
NO code-exec tool exists (house T3)."""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from proof_engine import ProofCore, Refusal, AGENT_TOOLS, deep_key_scan, BRAND

STATE = os.environ.get("ECLIPSE_PROOF_STATE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
PUB = os.environ.get("ECLIPSE_PROOF_HERALD_PUBKEY") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "herald_pubkey.pem")
core = ProofCore(STATE, open(PUB).read() if os.path.exists(PUB) else None)

def _s(desc):
    return {"type": "string", "description": desc}


def _t(desc):
    return {"type": ["integer", "string"], "description": desc + " Accepts an integer frame count or an 'HH:MM:SS:FF' timecode string at 30 fps; float seconds are refused."}


def _i(desc):
    return {"type": ["integer", "string"], "description": desc + " Accepts an integer or a decimal integer string."}


_P_SESSION = _s("Session id returned by project_open, e.g. 'sess_ab12cd34ef56'. Every tool except project_open requires a live session.")
_P_TIMELINE = _s("Timeline id inside the session's project, e.g. 'main'.")

# name -> (description, {param: schema}) — required = all params whose
# schema key does not end in '?', mirroring the dispatch contract.
TOOL_DEFS = {
 "project_open": (
  "Open a video-editing project and start an attested session: the project identity is read back from the editor and hash-attested before any work is accepted. Use this first — every other tool in this server requires the session_id it returns. Do NOT use it to check a project's current state mid-session; use project_attest for that. Behaviour: creates server-side session state (mutating); the only supported editor_class in this build is 'Resolve' — any other value is refused with ADAPTER_UNAVAILABLE, and an unknown project_ref is refused with PROJECT_NOT_FOUND. No network or signing key is involved on this surface. Returns {session_id, attestation: {project_id, editor_class, editor_version, project_identity_hash, evidence_grade}, brand}.",
  {"editor_class": _s("Editor class to open the project through. Only 'Resolve' is supported in this build."),
   "project_ref": _s("Project reference understood by the editor adapter, e.g. a project name or path ref. Unknown refs are refused with PROJECT_NOT_FOUND.")}),
 "project_attest": (
  "Re-attest the session's project identity and compare it against the attestation taken at project_open. Use this to prove the project on disk is still the project that was opened before relying on fingerprints or exports; do NOT use it to open a project — use project_open. Behaviour: read-only. Returns the fresh attestation object {project_id, editor_class, editor_version, project_identity_hash, attested_at, evidence_grade}; if the identity hash changed since open, the call is refused with PROJECT_IDENTITY_CHANGED.",
  {"session_id": _P_SESSION}),
 "timeline_read": (
  "Read the canonical form of one timeline in the session's project: tracks and every edit decision in canonical order. Use this to inspect exact timeline state; use timeline_fingerprint when you only need the hash to compare states. Behaviour: read-only. Returns the canonical timeline object for the requested timeline_id.",
  {"session_id": _P_SESSION, "timeline_id": _P_TIMELINE}),
 "timeline_fingerprint": (
  "Compute the canonical SHA-256 fingerprint of one timeline's exact current state. Use this to detect any change since approval or since a previous read; use timeline_read when you need the contents, not just the hash. Behaviour: read-only and recomputed live on every call. Returns {fingerprint, coverage_version, evidence_grade: 'LIVE_READBACK', computed_at}.",
  {"session_id": _P_SESSION, "timeline_id": _P_TIMELINE}),
 "export_presets_list": (
  "List the non-retired export presets available to this session, with each preset's id, pinned hash, and delivery spec. Use this to choose a preset_id for approval_request; there is no preset-creation tool on this agent surface. Behaviour: read-only. Returns {presets: [{preset_id, preset_hash, delivery_spec_id}]}.",
  {"session_id": _P_SESSION}),
 "template_list": (
  "List the non-retired templates available for an editor class, with each template's hash, version, name, and slot schema. Use this to find a template_hash and its required slots before template_instantiate. Behaviour: read-only. Returns {templates: [{template_hash, version, slot_schema, name}]}.",
  {"session_id": _P_SESSION,
   "editor_class": _s("Editor class whose template set to list. Only 'Resolve' templates exist in this build.")}),
 "media_import": (
  "Import one media source into the session project's media pool, recording its content hash and license state, so it can be placed with clip_place. Use this before placing any new media; do NOT use it for media already imported — reuse the media_id returned earlier. Behaviour: mutating, verified by readback before it reports success. license_state is mandatory: a missing state, or one missing source/license_class/scope/expiry, is refused with LICENSE_STATE_MISSING, and media whose license blocks export later blocks export_execute. A source_ref starting with 'missing' is refused with MEDIA_NOT_FOUND. Returns the media record {media_id, source_ref, content_hash, license_state, imported_by, imported_at}.",
  {"session_id": _P_SESSION,
   "source_ref": _s("Reference to the media source, e.g. 'pd/clip1.mp4'."),
   "license_state?": {"type": "object", "description": "License record for the media; although marked optional in the raw schema, the engine refuses without it. Must contain source, license_class, scope, and expiry (expiry as 'YYYY-MM-DD'). Scope must cover export (e.g. 'export' or 'all') for the media to be exportable."}}),
 "clip_place": (
  "Place an imported media clip onto a timeline track at a start frame, using a source in/out range. Use this for new placements; use clip_trim to change an existing placement's range or position. Behaviour: mutating, verified by readback. Overlapping an existing clip on the same track is refused with PLACEMENT_COLLISION; an unimported media_id is refused with MEDIA_NOT_FOUND. Returns the placement decision record {decision_id, decision_class: 'clip_placement', track_index, media_hash, start_frame, source_in, source_out, end_frame, enabled}.",
  {"session_id": _P_SESSION, "timeline_id": _P_TIMELINE,
   "media_id": _s("Media id returned by media_import, e.g. 'media_ab12cd34ef56'. Must already be imported."),
   "track_index": _i("Target track index, 0–3 inclusive; outside that range is refused with TRACK_OUT_OF_RANGE."),
   "start_frame": _t("Timeline frame where the clip starts."),
   "source_in": _t("First source frame used from the media."),
   "source_out": _t("Source frame where the used range ends (exclusive end of the placed segment; end_frame = start_frame + source_out - source_in).")}),
 "clip_trim": (
  "Change an existing clip placement's source in/out range and start frame. Use this to adjust a clip that clip_place already created; use clip_place for a new placement. Behaviour: mutating, verified by readback. source_out must be greater than source_in or the call is refused with TRIM_OUT_OF_RANGE; an unknown clip_ref is refused with CLIP_NOT_FOUND. Returns the updated placement decision record.",
  {"session_id": _P_SESSION, "timeline_id": _P_TIMELINE,
   "clip_ref": _s("decision_id of the existing placement, as returned by clip_place."),
   "source_in": _t("New first source frame."),
   "source_out": _t("New source end frame; must be greater than source_in."),
   "start_frame": _t("New timeline start frame for the clip.")}),
 "text_set": (
  "Create or replace a text item (caption/title) on a timeline track between two frames. Use item_ref 'NEW' to create an item, or an existing item's decision_id to replace its text. Use clip_place for media and marker_set for labelled markers instead. Behaviour: mutating, verified by readback. The target track must be a text-capable ('T' or 'V') track or the call is refused with TRACK_NOT_TEXT; an item_ref that is neither 'NEW' nor an existing decision is refused with TEXT_ITEM_NOT_FOUND. Returns the text decision record {decision_id, decision_class: 'text_content', track_index, text, start_frame, end_frame}.",
  {"session_id": _P_SESSION, "timeline_id": _P_TIMELINE,
   "track_index": _i("Target track index; the track must exist and be text-capable."),
   "item_ref": _s("Existing text decision_id to replace, or the literal 'NEW' to create a new item."),
   "text": _s("The exact text content to set."),
   "start_frame": _t("Timeline frame where the text appears."),
   "end_frame": _t("Timeline frame where the text stops being shown.")}),
 "template_instantiate": (
  "Instantiate a pinned template onto a timeline track, filling its slots with validated values. Use template_list first to get the template_hash and slot schema. Behaviour: mutating, verified by readback. The template artifact is re-hashed live: any drift from the pinned template_hash is refused with TEMPLATE_PIN_MISMATCH and no override exists. Slot values are checked against the schema — unknown slots, missing required slots, wrong types, over-length TEXT, or MEDIA_REF values that were not imported are refused with TEMPLATE_SLOT_SCHEMA_VIOLATION or TEMPLATE_SLOT_TYPE_MISMATCH. Returns {instance_id, template_hash, slot_values, timeline_ref}.",
  {"session_id": _P_SESSION, "timeline_id": _P_TIMELINE,
   "template_hash": _s("Pinned template hash as returned by template_list."),
   "track_index": _i("Target track index for the instance."),
   "start_frame": _t("Timeline frame where the instance starts; the instance spans 90 frames."),
   "slot_values": {"type": "object", "description": "Values keyed by slot_id, conforming to the template's slot_schema from template_list. MEDIA_REF slots take an imported media_id."}}),
 "marker_set": (
  "Set a labelled marker at one frame of a timeline, with an arbitrary payload attached. Use this to annotate a frame for later review; use text_set for viewer-visible text instead. Behaviour: mutating, verified by readback. Returns the marker decision record {decision_id, decision_class: 'markers', label, payload, start_frame, end_frame}.",
  {"session_id": _P_SESSION, "timeline_id": _P_TIMELINE,
   "frame": _t("Timeline frame the marker is pinned to."),
   "label": _s("Marker label; part of the marker's identity hash, so the same label at the same frame reproduces the same decision_id."),
   "payload": {"description": "Arbitrary JSON payload stored with the marker (any type)."}}),
 "approval_request": (
  "Request export approval for one timeline under one preset: the timeline is fingerprinted, license blockers across all placed media are computed, and a single-use approval record is created in state REQUESTED. Use this after editing is complete; the approval must then be signed on the operator surface (a separate process — this agent surface cannot sign) before export_execute can run. Behaviour: mutating (creates the approval record and moves the session to APPROVAL_PENDING). An unpinned preset_id is refused with PRESET_NOT_PINNED. Returns the approval record {approval_id, state: 'REQUESTED', fingerprint, preset_id, preset_hash, license_warning, ...} with the canonical timeline omitted.",
  {"session_id": _P_SESSION, "timeline_id": _P_TIMELINE,
   "preset_id": _s("Preset id from export_presets_list; must match a pinned preset.")}),
 "export_execute": (
  "Execute the export for a signed, active approval: verifies the Herald signature, approval state and expiry, preset hash, template pins, and license states, re-fingerprints the timeline, renders through the editor adapter, and re-fingerprints after rendering. Use this only with an approval_id that approval_request created and the operator surface signed; use approval_request to start the flow. Behaviour: mutating and single-use — success consumes the approval (state CONSUMED) and moves the session to EXPORTED. Any timeline change since approval is refused with TIMELINE_CHANGED_SINCE_APPROVAL (with a drift report); an unsigned, expired, or out-of-scope approval and any unknown/expired license state are refused by name. Returns {render_hash, attempt_id, render_ref, render_file: {duration_frames, container, codec, width, height, fps_rational}, verification: 'QUEUED_PROOF_PRINT', brand}.",
  {"session_id": _P_SESSION,
   "approval_id": _s("Approval id returned by approval_request, e.g. 'appr_ab12cd34ef56'. Must be signed and in state SIGNED_PINNED.")}),
 "session_close": (
  "Close the session and seal its production pack: attestation, approvals, refusals, exports, template pins, and license states are hashed into one pack written to disk. Use this when work is finished; after sealing, the session accepts no further work and a second close is refused with SESSION_ALREADY_SEALED. If an export happened but no verification pack exists yet, close is refused with EXPORT_UNVERIFIED. Behaviour: mutating and terminal for the session. Returns {production_pack: <sealed pack>, pack_hash}.",
  {"session_id": _P_SESSION}),
}

SCHEMAS = {
 "project_open": {"editor_class": "str", "project_ref": "str"},
 "project_attest": {"session_id": "str"}, "timeline_read": {"session_id": "str", "timeline_id": "str"},
 "timeline_fingerprint": {"session_id": "str", "timeline_id": "str"},
 "export_presets_list": {"session_id": "str"}, "template_list": {"session_id": "str", "editor_class": "str"},
 "media_import": {"session_id": "str", "source_ref": "str", "license_state": "dict?"},
 "clip_place": {"session_id": "str", "timeline_id": "str", "media_id": "str", "track_index": "int", "start_frame": "int", "source_in": "int", "source_out": "int"},
 "clip_trim": {"session_id": "str", "timeline_id": "str", "clip_ref": "str", "source_in": "int", "source_out": "int", "start_frame": "int"},
 "text_set": {"session_id": "str", "timeline_id": "str", "track_index": "int", "item_ref": "str", "text": "str", "start_frame": "int", "end_frame": "int"},
 "template_instantiate": {"session_id": "str", "timeline_id": "str", "template_hash": "str", "track_index": "int", "start_frame": "int", "slot_values": "dict"},
 "marker_set": {"session_id": "str", "timeline_id": "str", "frame": "int", "label": "str", "payload": "any"},
 "approval_request": {"session_id": "str", "timeline_id": "str", "preset_id": "str"},
 "export_execute": {"session_id": "str", "approval_id": "str"},
 "session_close": {"session_id": "str"},
}

TOOLS = [{"name": n, "description": desc,
          "inputSchema": {"type": "object",
                          "properties": {k.rstrip("?"): v for k, v in props.items()},
                          "required": [k.rstrip("?") for k in props if not k.endswith("?")]}}
         for n, (desc, props) in TOOL_DEFS.items()]

def dispatch(name, args):
    if name not in AGENT_TOOLS:
        return {"refusal_code": "SURFACE_TOOL_UNKNOWN", "detail": "", "brand": BRAND}
    deep_key_scan(args)
    core.reload_if_changed()
    fn = getattr(core, name)
    # only pass declared params; missing -> engine/Refusal via TypeError mapping
    kwargs = {k: args[k] for k in SCHEMAS[name] if k in args}
    missing = [k for k, v in SCHEMAS[name].items() if not v.endswith("?") and k not in kwargs]
    if missing: return {"refusal_code": "SCHEMA_MISSING_PARAM", "detail": ",".join(missing), "brand": BRAND}
    try:
        return fn(**kwargs)
    except Refusal as r:
        p = r.payload()
        if getattr(r, "stage", None): p["stage"] = r.stage
        return p

def main():
    for line in sys.stdin:
        line = line.strip()
        if not line: continue
        try: req = json.loads(line)
        except Exception: continue
        method, rid = req.get("method"), req.get("id")
        if method == "initialize":
            res = {"protocolVersion": "2025-03-26", "serverInfo": {"name": "eclipse-proof", "version": "0.1.0"}, "capabilities": {"tools": {}}}
        elif method == "ping": res = {}
        elif method == "tools/list": res = {"tools": TOOLS}
        elif method == "tools/call":
            p = req.get("params", {})
            try: out = dispatch(p.get("name"), p.get("arguments") or {})
            except Refusal as r: out = r.payload()
            except Exception as e: out = {"refusal_code": "INTERNAL_GUARD", "detail": type(e).__name__, "brand": BRAND}
            res = {"content": [{"type": "text", "text": json.dumps(out)}], "isError": "refusal_code" in out}
        else:
            res = {}
        if rid is not None:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": res}) + "\n"); sys.stdout.flush()

if __name__ == "__main__": main()
# Eclipse Proof MCP · © 2026 Cyan Eclipse Technologies LLC · cetech.ai
