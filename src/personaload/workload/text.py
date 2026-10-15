"""Seeded synthetic chat text in English or Korean (SPEC_B §7.2-7.3).

Token counts are approximated from characters (en 4.0, ko 1.5 chars/token). The server's
`usage` is what gets measured; this only sizes the generated text.
"""

from __future__ import annotations

import math
import random

CHARS_PER_TOKEN = {"en": 4.0, "ko": 1.5}

WORDS = {
    "en": """
        hey so i was wondering if you could tell me about the old harbor again
        why do you always smile when it rains last night the stars looked different
        can we go back to the library tomorrow maybe i should bring tea this time
        what happened after the storm did the ship ever return tell me a secret
        i think the map is wrong somehow the forest feels quiet today honestly
        that story made me laugh do you remember our first adventure please be careful
        the market was crowded and i lost my way what would you do in my place
        your garden looks beautiful in the morning light i found a strange key
        should we trust the traveler from the north it sounds dangerous but exciting
    """.split(),
    "ko": """
        오늘 혹시 그 오래된 항구 이야기 다시 해줄래 비가 오면 왜 항상 웃는 거야
        어젯밤 별이 좀 달라 보였어 내일 같이 도서관에 갈까 이번에는 내가 차를 가져갈게
        폭풍이 지나고 나서 무슨 일이 있었어 그 배는 결국 돌아왔어 비밀 하나만 알려줘
        지도가 뭔가 잘못된 것 같아 오늘은 숲이 유난히 조용하네 솔직히 그 얘기 너무 웃겼어
        우리 첫 모험 기억나 조심해야 해 시장이 너무 붐벼서 길을 잃었어
        네가 나라면 어떻게 할 거야 아침 햇살에 정원이 정말 예쁘다 이상한 열쇠를 찾았어
        북쪽에서 온 여행자를 믿어도 될까 위험해 보이지만 조금 설레기도 해 고양이가 또 사라졌어
    """.split(),
}

_ENDINGS = (".", ".", ".", "?", "!")
_SENTENCE_WORDS = (4, 12)


def target_chars(tokens: int, language: str) -> int:
    return max(1, round(tokens * CHARS_PER_TOKEN[language]))


def approx_tokens(text: str, language: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN[language])


def _pick(rng: random.Random, seq: tuple[str, ...] | list[str]) -> str:
    return seq[math.floor(rng.random() * len(seq))]


def _sentence_length(rng: random.Random) -> int:
    lo, hi = _SENTENCE_WORDS
    return lo + math.floor(rng.random() * (hi - lo + 1))


def make_text(rng: random.Random, tokens: int, language: str) -> str:
    """Sentences of bank words until the approximate token target is reached."""
    bank = WORDS[language]
    goal = target_chars(tokens, language)
    words: list[str] = []
    length = 0
    remaining_in_sentence = _sentence_length(rng)
    while length < goal:
        word = _pick(rng, bank)
        if language == "en" and (not words or words[-1][-1] in ".?!"):
            word = word.capitalize()
        remaining_in_sentence -= 1
        if remaining_in_sentence == 0:
            word += _pick(rng, _ENDINGS)
            remaining_in_sentence = _sentence_length(rng)
        length += len(word) + (1 if words else 0)
        words.append(word)
    text = " ".join(words)
    return text if text[-1] in ".?!" else text + "."
