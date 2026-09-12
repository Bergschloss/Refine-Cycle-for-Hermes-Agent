# What a bigger memory costs

Derived from runs 6 and 7, not from a separate experiment. Those runs held the task,
the lesson and the probes fixed and varied only the length of the memory note, so the
token records are a natural measurement of what memory costs.

Median over 12 cells per level. Run 6, lesson first:

| note chars | fresh input | cache read | output |
|---|---|---|---|
| 161 | 4,220 | 12,800 | 388 |
| 1,119 | 5,203 | 13,312 | 396 |
| 2,246 | 6,351 | 13,824 | 406 |
| 4,432 | 6,664 | 15,872 | 396 |
| 8,811 | 7,285 | 19,456 | 418 |

Run 7, lesson in the middle:

| note chars | fresh input | cache read | output |
|---|---|---|---|
| 161 | 4,542 | 12,288 | 360 |
| 1,119 | 4,764 | 13,312 | 392 |
| 2,246 | 5,811 | 13,824 | 423 |
| 4,432 | 5,224 | 16,896 | 409 |
| 8,811 | 7,392 | 19,456 | 403 |

## The shape of it

Growing the note from 161 to 8,811 characters adds about 3,100 tokens of text. Fresh
input grew by 3,065. That is one copy. Each session made six API calls, so a note
charged per call would have added six copies; it did not. The note is billed fresh once
and read from cache thereafter.

Output does not move with memory at all: 388, 396, 406, 396, 418 across the five levels
is noise. Since output is the most expensive token, the most expensive part of a request
is unaffected by how much memory it carries.

## Cost in percent

Exact prices differ by route, so the table gives four pricing shapes. The answer barely
moves between them. Output is held at its mean, since it does not vary with memory.

| memory change | c10/o4 | c10/o8 | c25/o4 | c25/o8 |
|---|---|---|---|---|
| 2,200 to 4,400 (Hermes default to plugin floor) | +5.5% | +4.7% | +7.2% | +6.3% |
| 4,400 to 8,800 (double the floor) | +9.9% | +8.5% | +12.4% | +11.0% |
| 2,200 to 8,800 (four times the default) | +16.0% | +13.7% | +20.5% | +18.0% |

c10 and c25: cache reads billed at 10% or 25% of fresh input. o4 and o8: output billed
at four or eight times fresh input.

Doubling the memory costs roughly a tenth of the bill. Quadrupling it costs roughly a
fifth. One model, one route, sessions of six calls; a longer session spreads the one
fresh copy over more calls and the share falls further.
