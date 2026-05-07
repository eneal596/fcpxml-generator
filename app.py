"""
FCPXML Generator service for Rough Cut Automation - v4.

Changes from v3:
1. Gap clips for ai_gen / human_review now have visible titled placeholder
   slugs on the timeline (so editors see what's needed without leaving Premiere).
2. Multi-sequence output: scripts with multiple Hook variations produce one
   sequence per hook (named [tabName]-V1, V2, etc.), each containing
   that hook's portion of the avatar + the shared body section.

Hook detection: any beat whose `section` field starts with "Hook" (case-insensitive)
is treated as a hook. Beats whose section does not start with "Hook" are body.

If a script has zero hook beats or only one, the output is a single sequence
named [tabName].

POST /generate
Body: { projectSettings, outcomes, totalAudioDuration }
Returns: { fcpxml, filename, stats }
"""

from flask import Flask, request, jsonify
import opentimelineio as otio
from opentimelineio.opentime import RationalTime, TimeRange
import os
import tempfile
import re

app = Flask(__name__)

FRAME_RATE = 30
PORTRAIT = (1080, 1920)
LANDSCAPE = (1920, 1080)
SQUARE = (1080, 1080)


def get_dimensions(orientation):
    if orientation == "Landscape":
        return LANDSCAPE
    elif orientation == "Square":
        return SQUARE
    return PORTRAIT


def seconds_to_rational_time(seconds, frame_rate=FRAME_RATE):
    return RationalTime(round(seconds * frame_rate), frame_rate)


def make_external_reference(filename, asset_duration_seconds=600):
    if not filename.lower().endswith((".mp4", ".mov", ".m4v")):
        filename = filename + ".mp4"
    target_url = f"./Footage/{filename}"
    available_range = TimeRange(
        start_time=RationalTime(0, FRAME_RATE),
        duration=seconds_to_rational_time(asset_duration_seconds),
    )
    return otio.schema.ExternalReference(
        target_url=target_url,
        available_range=available_range,
    )


def make_avatar_reference(total_duration):
    available_range = TimeRange(
        start_time=RationalTime(0, FRAME_RATE),
        duration=seconds_to_rational_time(total_duration),
    )
    return otio.schema.ExternalReference(
        target_url="./avatar.mp4",
        available_range=available_range,
    )


def is_hook_beat(beat):
    """Return True if the beat's section indicates it's a hook."""
    section = (beat.get("section") or "").strip().lower()
    return section.startswith("hook")


def make_placeholder_clip(label, duration_seconds):
    """
    Build a 'placeholder' clip on V1 — a clip that points at a synthetic
    file URL Premiere won't find, with a name that displays on the timeline.
    The result: a red 'media offline' clip with the label text visible.
    
    This is more useful than a Gap because Premiere shows the name on the
    clip in the timeline, and the editor can right-click and replace.
    """
    # Sanitise label for use as a filename (Premiere displays this)
    sanitised = re.sub(r'[^\w\s\-\[\]]', '_', label)[:100].strip()
    if not sanitised:
        sanitised = "PLACEHOLDER"
    fake_filename = f"{sanitised}.mp4"
    
    available_range = TimeRange(
        start_time=RationalTime(0, FRAME_RATE),
        duration=seconds_to_rational_time(duration_seconds),
    )
    media_ref = otio.schema.ExternalReference(
        target_url=f"./Footage/_PLACEHOLDERS/{fake_filename}",
        available_range=available_range,
    )
    return otio.schema.Clip(
        name=label[:200],  # Premiere shows clip name in the timeline
        media_reference=media_ref,
        source_range=TimeRange(
            start_time=RationalTime(0, FRAME_RATE),
            duration=seconds_to_rational_time(duration_seconds),
        ),
    )


def close_inter_beat_gaps(outcomes, max_gap_to_close=2.0):
    """
    Mutate outcome beats so consecutive clips have no visible gap between them.
    For each pair of consecutive beats with a gap less than max_gap_to_close seconds,
    move the gap split-point to the midpoint: extend the earlier beat\'s endTime
    and pull the later beat\'s startTime forward to that midpoint.
    
    Larger gaps (e.g., a long pause in the audio) are left alone — they probably
    represent intentional silence.
    """
    sorted_outcomes = sorted(
        outcomes, key=lambda o: (o.get("beat", {}).get("startTime") or 0)
    )
    
    for i in range(len(sorted_outcomes) - 1):
        current = sorted_outcomes[i]
        next_ = sorted_outcomes[i + 1]
        
        current_beat = current.get("beat") or {}
        next_beat = next_.get("beat") or {}
        
        cur_end = current_beat.get("endTime")
        next_start = next_beat.get("startTime")
        
        if cur_end is None or next_start is None:
            continue
        
        gap = next_start - cur_end
        if 0 < gap <= max_gap_to_close:
            midpoint = cur_end + (gap / 2.0)
            current_beat["endTime"] = midpoint
            next_beat["startTime"] = midpoint
            # Recompute durations to match
            current_beat["duration"] = midpoint - (current_beat.get("startTime") or 0)
            next_beat["duration"] = (next_beat.get("endTime") or midpoint) - midpoint
    
    return sorted_outcomes


def build_track_for_outcomes(outcomes, asset_ids, segment_start, segment_end):
    """
    Build a video track containing B-roll/placeholder clips for outcomes
    whose timestamps fall within [segment_start, segment_end].
    
    Timestamps in the track are RELATIVE to segment_start (so the track
    starts at 0:00 regardless of where in the original audio it lives).
    """
    track = otio.schema.Track(name="V1 — B-roll", kind=otio.schema.TrackKind.Video)
    
    # Filter outcomes to this segment
    segment_outcomes = []
    for o in outcomes:
        beat = o.get("beat", {}) or {}
        bs = beat.get("startTime") or 0
        be = beat.get("endTime") or bs
        # Include outcomes that overlap with the segment
        if be <= segment_start or bs >= segment_end:
            continue
        segment_outcomes.append(o)
    
    segment_outcomes.sort(key=lambda o: (o.get("beat", {}).get("startTime") or 0))
    
    cursor = 0.0  # relative to segment_start
    for outcome in segment_outcomes:
        beat = outcome.get("beat", {}) or {}
        original_start = beat.get("startTime") or 0
        original_end = beat.get("endTime") or original_start
        
        # Clip the beat's range to the segment
        effective_start = max(original_start, segment_start) - segment_start
        effective_end = min(original_end, segment_end) - segment_start
        duration = max(effective_end - effective_start, 1.0 / FRAME_RATE)
        
        # Fill any gap before this beat
        if effective_start > cursor + 0.001:
            gap_duration = effective_start - cursor
            filler = otio.schema.Gap(
                name="(silence)",
                source_range=TimeRange(
                    start_time=RationalTime(0, FRAME_RATE),
                    duration=seconds_to_rational_time(gap_duration),
                ),
            )
            track.append(filler)
            cursor = effective_start
        
        outcome_type = outcome.get("outcome")
        clip_duration = seconds_to_rational_time(duration)
        
        if outcome_type == "match":
            filename = outcome.get("chosenFilename", "")
            if filename:
                clip = otio.schema.Clip(
                    name=filename,
                    media_reference=make_external_reference(filename),
                    source_range=TimeRange(
                        start_time=RationalTime(0, FRAME_RATE),
                        duration=clip_duration,
                    ),
                )
                track.append(clip)
                cursor = effective_start + duration
                continue
        
        # Build a labelled placeholder clip
        if outcome_type == "ai_gen":
            label = (
                f"[AI GEN] "
                f"{outcome.get('aiGenPrompt') or beat.get('visualDirection') or 'Generate B-roll'}"
            )
        elif outcome_type == "human_review":
            label = (
                f"[HUMAN REVIEW] "
                f"{outcome.get('humanReviewReason') or beat.get('visualDirection') or 'Needs producer attention'}"
            )
        else:
            label = f"[NO MATCH] beat {beat.get('beatId')}"
        
        placeholder = make_placeholder_clip(label, duration)
        track.append(placeholder)
        cursor = effective_start + duration
    
    # Pad to segment end if needed
    segment_total = segment_end - segment_start
    if cursor < segment_total - 0.001:
        pad_duration = segment_total - cursor
        pad = otio.schema.Gap(
            name="(silence)",
            source_range=TimeRange(
                start_time=RationalTime(0, FRAME_RATE),
                duration=seconds_to_rational_time(pad_duration),
            ),
        )
        track.append(pad)
    
    return track


def build_avatar_audio_track(segment_start, segment_end):
    """Avatar audio for the given time segment, trimmed in/out points."""
    track = otio.schema.Track(name="A1 — Avatar Audio", kind=otio.schema.TrackKind.Audio)
    duration = segment_end - segment_start
    
    media_ref = make_avatar_reference(segment_end)  # available range goes up to segment_end
    clip = otio.schema.Clip(
        name="Avatar Audio",
        media_reference=media_ref,
        source_range=TimeRange(
            start_time=seconds_to_rational_time(segment_start),
            duration=seconds_to_rational_time(duration),
        ),
    )
    track.append(clip)
    return track


def build_avatar_video_track(segment_start, segment_end):
    """Avatar video for talking_head_overlay, trimmed in/out points."""
    track = otio.schema.Track(name="V2 — Avatar Overlay", kind=otio.schema.TrackKind.Video)
    duration = segment_end - segment_start
    
    media_ref = make_avatar_reference(segment_end)
    clip = otio.schema.Clip(
        name="Avatar",
        media_reference=media_ref,
        source_range=TimeRange(
            start_time=seconds_to_rational_time(segment_start),
            duration=seconds_to_rational_time(duration),
        ),
    )
    track.append(clip)
    return track


def build_timeline_for_segment(
    name, outcomes, video_type, segment_start, segment_end
):
    """
    Build a single OTIO Timeline for a contiguous time segment of the audio.
    Used for both single-sequence and per-hook-variation output.
    """
    timeline = otio.schema.Timeline(name=name)
    timeline.global_start_time = RationalTime(0, FRAME_RATE)
    
    # Build asset_ids dict (used by build_track_for_outcomes via shared logic).
    # Currently asset_ids isn't actually used by that function — references
    # are made on the fly. Keeping it as a placeholder.
    asset_ids = {}
    
    # V1 — B-roll & placeholders for this segment
    v1 = build_track_for_outcomes(outcomes, asset_ids, segment_start, segment_end)
    timeline.tracks.append(v1)
    
    # V2 — Avatar overlay (talking_head_overlay only)
    if video_type == "talking_head_overlay":
        v2 = build_avatar_video_track(segment_start, segment_end)
        timeline.tracks.append(v2)
    
    # A1 — Avatar audio
    a1 = build_avatar_audio_track(segment_start, segment_end)
    timeline.tracks.append(a1)
    
    return timeline


def identify_hook_segments(outcomes, total_duration):
    """
    Inspect outcomes to find hook beats. Return a list of (hook_name, hook_start, hook_end)
    tuples, plus (body_start, body_end). Hook beats are those whose section starts with 'Hook'.
    
    Convention: hooks are at the START of the script. Body begins after the last hook beat.
    Each hook stands alone — its segment is from that hook's startTime to its endTime.
    The body segment is from the first non-hook beat's start to the end of audio.
    """
    sorted_outcomes = sorted(
        outcomes, key=lambda o: (o.get("beat", {}).get("startTime") or 0)
    )
    
    hooks = []
    body_start = None
    
    for outcome in sorted_outcomes:
        beat = outcome.get("beat", {}) or {}
        section = (beat.get("section") or "").strip()
        bs = beat.get("startTime") or 0
        be = beat.get("endTime") or bs
        
        if is_hook_beat(beat):
            # Each hook is its own segment
            hooks.append({
                "name": section,  # e.g. "Hook V1"
                "start": bs,
                "end": be,
            })
        else:
            if body_start is None:
                body_start = bs
            # Don't break — continue to allow more hook beats interspersed,
            # though they shouldn't normally appear after body starts.
    
    if body_start is None:
        # No body beats found — entire script is hooks (unusual)
        body_start = total_duration
    
    body_end = total_duration
    
    return hooks, body_start, body_end


def timeline_to_fcpxml(timeline):
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".fcpxml", delete=False, encoding="utf-8"
    ) as tmp:
        tmp_path = tmp.name
    try:
        otio.adapters.write_to_file(timeline, tmp_path, adapter_name="fcp_xml")
        with open(tmp_path, "r", encoding="utf-8") as f:
            return f.read()
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


def build_combined_fcpxml(sequences_xml_list, project_name):
    """
    OTIO's fcp_xml adapter writes one project per file. To get multiple sequences
    in one file, we generate each timeline separately, then merge their <sequence>
    elements into a single <project>.
    
    All file/clipitem IDs are renumbered to be globally unique across sequences,
    otherwise Premiere silently discards duplicates.
    """
    if len(sequences_xml_list) == 1:
        return sequences_xml_list[0]
    
    # Pull each <sequence>...</sequence> block, then renumber its IDs
    sequence_blocks = []
    id_offset = 0
    for i, xml in enumerate(sequences_xml_list):
        match = re.search(r'(<sequence[^>]*>.*?</sequence>)', xml, re.DOTALL)
        if not match:
            continue
        block = match.group(1)
        
        # Find all numeric IDs in this block (id="sequence-1", id="clipitem-3", id="file-2")
        # and offset them by id_offset to make them globally unique
        # Strategy: find the highest ID in this block, then shift everything up
        all_ids = re.findall(r'id="(?:sequence|clipitem|file)-(\d+)"', block)
        max_id_in_block = max([int(x) for x in all_ids]) if all_ids else 0
        
        def shift_id(m):
            prefix = m.group(1)
            num = int(m.group(2))
            return f'id="{prefix}-{num + id_offset}"'
        
        # Only shift the FIRST occurrence (the definition); references to file-X
        # within the same sequence need to stay consistent. Use a simpler regex
        # that catches both definitions and references:
        block = re.sub(
            r'id="(sequence|clipitem|file)-(\d+)"',
            shift_id,
            block,
        )
        # Also handle <file id="file-X"/> reference style — already covered above
        
        sequence_blocks.append(block)
        id_offset += max_id_in_block + 1  # next sequence starts above this one
    
    if not sequence_blocks:
        return sequences_xml_list[0]
    
    combined_children = "\n            ".join(sequence_blocks)
    
    combined_xml = f"""<?xml version="1.0" ?>
<xmeml version="4">
    <project>
        <name>{project_name}</name>
        <children>
            {combined_children}
        </children>
    </project>
</xmeml>
"""
    return combined_xml


def build_fcpxml(project_settings, outcomes, total_audio_duration):
    tab_name = project_settings.get("tabName", "Untitled")
    video_type = project_settings.get("videoType", "narrated_story")
    
    # Close small gaps between consecutive beats so the timeline has no black frames
    outcomes = close_inter_beat_gaps(outcomes, max_gap_to_close=2.0)
    
    hooks, body_start, body_end = identify_hook_segments(outcomes, total_audio_duration)
    
    # Decide output mode:
    # - 0 or 1 hook → single sequence covering the whole audio
    # - 2+ hooks → one sequence per hook, each containing [hook + body]
    if len(hooks) <= 1:
        timeline = build_timeline_for_segment(
            name=tab_name,
            outcomes=outcomes,
            video_type=video_type,
            segment_start=0.0,
            segment_end=total_audio_duration,
        )
        return timeline_to_fcpxml(timeline)
    
    # Multi-hook: build one sequence per hook variation
    sequences_xml = []
    for i, hook in enumerate(hooks, start=1):
        seq_name = f"{tab_name}-V{i}"
        # This sequence contains: hook segment, then body segment
        # We build them as two consecutive ranges by passing all outcomes and
        # filtering, but since segments aren't contiguous (hook ends at e.g. 3.18,
        # body starts at e.g. 21.12), we need to handle that.
        #
        # Strategy: build a single timeline whose audio is the hook + body
        # concatenated. The avatar source-range trick won't work for non-contiguous
        # ranges in a single clip, so we add TWO avatar audio clips back-to-back
        # in one track.
        timeline = build_concatenated_timeline(
            name=seq_name,
            outcomes=outcomes,
            video_type=video_type,
            segments=[
                (hook["start"], hook["end"]),
                (body_start, body_end),
            ],
        )
        sequences_xml.append(timeline_to_fcpxml(timeline))
    
    return build_combined_fcpxml(sequences_xml, tab_name)


def build_concatenated_timeline(name, outcomes, video_type, segments):
    """
    Build a timeline whose A1/V2 tracks contain multiple avatar clips back-to-back
    (one per segment, each with its own source in/out points), and whose V1 track
    contains B-roll/placeholders for all outcomes within those segments.
    
    The segments are concatenated — segment 2 starts at timeline time = duration(segment 1).
    """
    timeline = otio.schema.Timeline(name=name)
    timeline.global_start_time = RationalTime(0, FRAME_RATE)
    
    # ── A1: Avatar audio, one clip per segment, back-to-back ──
    audio_track = otio.schema.Track(name="A1 — Avatar Audio", kind=otio.schema.TrackKind.Audio)
    for seg_start, seg_end in segments:
        seg_duration = seg_end - seg_start
        media_ref = make_avatar_reference(seg_end)
        clip = otio.schema.Clip(
            name="Avatar Audio",
            media_reference=media_ref,
            source_range=TimeRange(
                start_time=seconds_to_rational_time(seg_start),
                duration=seconds_to_rational_time(seg_duration),
            ),
        )
        audio_track.append(clip)
    
    # ── V2: Avatar overlay, same structure as audio (talking_head_overlay only) ──
    video_overlay_track = None
    if video_type == "talking_head_overlay":
        video_overlay_track = otio.schema.Track(
            name="V2 — Avatar Overlay", kind=otio.schema.TrackKind.Video
        )
        for seg_start, seg_end in segments:
            seg_duration = seg_end - seg_start
            media_ref = make_avatar_reference(seg_end)
            clip = otio.schema.Clip(
                name="Avatar",
                media_reference=media_ref,
                source_range=TimeRange(
                    start_time=seconds_to_rational_time(seg_start),
                    duration=seconds_to_rational_time(seg_duration),
                ),
            )
            video_overlay_track.append(clip)
    
    # ── V1: B-roll/placeholders for outcomes within these segments,
    # remapped to concatenated timeline positions ──
    v1_track = otio.schema.Track(name="V1 — B-roll", kind=otio.schema.TrackKind.Video)
    
    # Walk each segment, remapping outcome timestamps
    timeline_cursor = 0.0
    for seg_start, seg_end in segments:
        seg_duration = seg_end - seg_start
        
        # Find outcomes within this segment
        segment_outcomes = sorted(
            [
                o for o in outcomes
                if (o.get("beat", {}) or {}).get("startTime") is not None
                and ((o.get("beat", {}).get("endTime") or 0) > seg_start)
                and ((o.get("beat", {}).get("startTime") or 0) < seg_end)
            ],
            key=lambda o: (o.get("beat", {}).get("startTime") or 0),
        )
        
        cursor_in_seg = 0.0
        for outcome in segment_outcomes:
            beat = outcome.get("beat", {}) or {}
            original_start = beat.get("startTime") or 0
            original_end = beat.get("endTime") or original_start
            
            effective_start = max(original_start, seg_start) - seg_start
            effective_end = min(original_end, seg_end) - seg_start
            duration = max(effective_end - effective_start, 1.0 / FRAME_RATE)
            
            if effective_start > cursor_in_seg + 0.001:
                gap_dur = effective_start - cursor_in_seg
                filler = otio.schema.Gap(
                    name="(silence)",
                    source_range=TimeRange(
                        start_time=RationalTime(0, FRAME_RATE),
                        duration=seconds_to_rational_time(gap_dur),
                    ),
                )
                v1_track.append(filler)
                cursor_in_seg = effective_start
            
            outcome_type = outcome.get("outcome")
            
            if outcome_type == "match":
                filename = outcome.get("chosenFilename", "")
                if filename:
                    clip = otio.schema.Clip(
                        name=filename,
                        media_reference=make_external_reference(filename),
                        source_range=TimeRange(
                            start_time=RationalTime(0, FRAME_RATE),
                            duration=seconds_to_rational_time(duration),
                        ),
                    )
                    v1_track.append(clip)
                    cursor_in_seg = effective_start + duration
                    continue
            
            if outcome_type == "ai_gen":
                label = (
                    f"[AI GEN] "
                    f"{outcome.get('aiGenPrompt') or beat.get('visualDirection') or 'Generate B-roll'}"
                )
            elif outcome_type == "human_review":
                label = (
                    f"[HUMAN REVIEW] "
                    f"{outcome.get('humanReviewReason') or beat.get('visualDirection') or 'Needs producer attention'}"
                )
            else:
                label = f"[NO MATCH] beat {beat.get('beatId')}"
            
            placeholder = make_placeholder_clip(label, duration)
            v1_track.append(placeholder)
            cursor_in_seg = effective_start + duration
        
        # Pad segment end if needed
        if cursor_in_seg < seg_duration - 0.001:
            pad_dur = seg_duration - cursor_in_seg
            pad = otio.schema.Gap(
                name="(silence)",
                source_range=TimeRange(
                    start_time=RationalTime(0, FRAME_RATE),
                    duration=seconds_to_rational_time(pad_dur),
                ),
            )
            v1_track.append(pad)
        
        timeline_cursor += seg_duration
    
    # Append tracks in order: V1, V2 (if exists), A1
    timeline.tracks.append(v1_track)
    if video_overlay_track:
        timeline.tracks.append(video_overlay_track)
    timeline.tracks.append(audio_track)
    
    return timeline


@app.route("/generate", methods=["POST"])
def generate():
    try:
        data = request.get_json(force=True)
        project_settings = data.get("projectSettings", {})
        outcomes = data.get("outcomes", [])
        total_duration = data.get("totalAudioDuration", 0)
        
        if not outcomes:
            return jsonify({"error": "No outcomes provided"}), 400
        if not total_duration:
            return jsonify({"error": "totalAudioDuration is required"}), 400
        
        fcpxml = build_fcpxml(project_settings, outcomes, total_duration)
        filename = f"{project_settings.get('tabName', 'output')}.xml"
        
        # Identify hooks for stats
        hooks, _, _ = identify_hook_segments(outcomes, total_duration)
        
        return jsonify({
            "fcpxml": fcpxml,
            "filename": filename,
            "stats": {
                "outcomeCount": len(outcomes),
                "matchCount": sum(1 for o in outcomes if o.get("outcome") == "match"),
                "gapCount": sum(
                    1 for o in outcomes if o.get("outcome") in ("ai_gen", "human_review")
                ),
                "hookCount": len(hooks),
                "sequenceCount": len(hooks) if len(hooks) > 1 else 1,
                "totalDuration": total_duration,
            },
        })
    except Exception as e:
        import traceback
        return jsonify({
            "error": str(e),
            "type": type(e).__name__,
            "traceback": traceback.format_exc()[-1500:],
        }), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "service": "fcpxml-generator",
        "version": "v5-fixed-ids-no-gaps",
        "otio_version": otio.__version__,
    })


@app.route("/", methods=["GET"])
def root():
    return jsonify({
        "service": "FCPXML Generator",
        "version": "v5 (Fixed IDs + Closed Gaps)",
        "endpoints": {
            "POST /generate": "Generate FCPXML from beat outcomes",
            "GET /health": "Health check",
        },
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
