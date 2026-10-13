import re


FILLERS = {
    "um",
    "uh",
    "actually",
    "basically",
    "literally",
    "like",
}


def calculate_speech_metrics(transcript, duration_seconds, words=None):
    tokens = re.findall(r"[A-Za-z']+", transcript.lower())
    duration = max(0, float(duration_seconds or 0))
    minutes = duration / 60 if duration else 0
    filler_count = sum(token in FILLERS for token in tokens)
    pause_seconds = 0.0
    if words:
        for previous, current in zip(words, words[1:]):
            gap = max(0, float(current["start"]) - float(previous["end"]))
            if gap >= 0.7:
                pause_seconds += gap
    return {
        "word_count": len(tokens),
        "words_per_minute": round(len(tokens) / minutes, 1) if minutes else 0,
        "filler_count": filler_count,
        "filler_ratio": round(filler_count / len(tokens), 4) if tokens else 0,
        "pause_seconds": round(pause_seconds, 2),
        "pause_ratio": round(pause_seconds / duration, 4) if duration else 0,
        "duration_seconds": round(duration, 2),
    }
