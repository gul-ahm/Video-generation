# Script format

Write the script in a plain text file (`.md` or `.txt`) and upload it on the dashboard under **My own script**.
`samples/TEMPLATE.md` is a ready file to copy. The finished Flannan and Mary Celeste scripts are in `samples/` too.

## Lines

| Line | Needed | What it does |
|---|---|---|
| `# Title` | yes, first line | The video title and the output folder name |
| `thumb: TEXT` | no | Big words on the thumbnail (3 to 5 words) |
| `## Chapter name` | yes, one per chapter | Starts a chapter; a new chapter opens with a sparkle |
| `card: Name` | no | Chapter name shown on screen (default: the chapter name) |
| `visuals: a | b | c` | no | Search words for real archive pictures, split by `|` |
| `> picture` | no | One picture for the narration below it, until the next `>` line |
| any other line | yes | Narration, read out word for word |
| blank line | no | New paragraph, a short pause in the voice |

## Tips

- About **188 words make one minute** of video with this voice. 1,900 words is about 10 minutes.
- 4 to 8 chapters work well. A chapter of 250 to 500 words is 1.5 to 3 minutes.
- Picture lines: start with "Wide shot of", name the people (adults), their clothes and the year. One picture for
  every 30 to 45 words looks good.
- Leave out all picture lines of a chapter and Gemini writes them. Your file is kept as `script_original.md`.
- Write only facts you can show a source for. Cards, captions and dates are taken from your sentences, so a wrong
  sentence becomes a wrong card.
- Do not use em dashes or quotes inside the narration; plain sentences read best.

## What the system does with it

1. Voice in your cloned voice, with the exact time of every word.
2. At the same time, Gemini plans the edit: which card or caption lands on which word, the mood and sounds.
3. Pictures, character movement, cards, music and effects, render.
4. `final.mp4`, `edit_plan.md` (every decision and why), `qa.md` (10 checks), `youtube.txt` and subtitles.
