# Documentary maker

Title ya headline do, system khud research karke 20 se 25 minute ki documentary banata hai.

    python documentary/doc.py "The Fall of Constantinople 1453" 22

| Step | File | Kya hota hai |
|---|---|---|
| 1. Research | `research.py` | 12 alag sawaalon se web aur news search, lagbhag 40 sources poore padhe jaate hain (har site se zyada se zyada 3). Wikipedia sirf ek source hai |
| 2. Claims | `research.py` | Har source se facts nikalte hain, source ke apne shabdon ke saath. Jo quote page me nahi milta wo fact hata diya jaata hai. 2 ya zyada sites par mila fact "confirmed", ek par "reported", ilzaam "alleged" |
| 3. Saboot | `assets.py` | Har article ka headline par screenshot. Film me ye source card ban kar aata hai, outlet aur tareekh ke saath |
| 4. Script | `write.py` | Sirf checked claims se likhi jaati hai, 188 shabd per minute ke hisaab se. Chhota chapter wapas bheja jaata hai |
| 5. Film | `../make.py` | Aapki awaaz, Kaggle par asli photo jaisi pictures, cards, maps, effects, music, YouTube pack |

Nateeja `output/<title>/` me: `final.mp4`, `sources.md` (saare sources aur claims), `research/` (padha hua text,
claims.json, screenshots), `script.md`, `youtube_pack.md`.

`RESEARCH_ONLY=1` lagane se film nahi banti, sirf research aur script: pehle `sources.md` aur `script.md` padh lo.
`DOC_STYLE=cinematic` purani ladaiyon ke liye, default `documentary` (asli photo jaisa).
