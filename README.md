# AK Voice Studio

Gemini TTS par bana local voice studio (ElevenLabs jaisa layout):
1. **Text to Speech** — script likho, Generate dabao. WAV + SRT milti hai, waveform player ke saath.
2. **Podcast** — 2 speakers, har speaker apni voice par lock.
3. **Subtitles** — audio/video se SRT, ya purani SRT ki script badlo (Roman / Hindi / Urdu).
4. **History** — har generation save; dobara sunein, pauses/script badlein, download karein.
5. **Voice Lab / Voice Agent** — voice design, cloning, live baat-cheet.

## Naya kya hai (v2)
- **Voice consistency:** lambi script ab chhote hisson (default 900 characters) mein jaati hai. Har hisse mein *wahi* voice, language aur speaker bindings jaati hain, is liye beech mein accent nahi badalta. Settings → "Voice consistency" slider (0 = purana tareeqa, ek request).
- **Pauses (khali jagah):** lambi khamoshi khud chhoti hoti hai — max pause, shuru/aakhir ki khamoshi aur threshold sliders se control. Generate ke baad player ke ⚙ se badlo; dobara TTS call nahi lagti (original audio `outputs/raw/` mein safe rehti hai). Filmora mein trim ki zaroorat nahi.
- **Subtitle script:** Original / Roman / हिंदी / اردو. Urdu SRT right-to-left marks (RLM) ke saath banti hai taake lines aur punctuation sahi taraf rahein. Conversion Gemini text model (newest Flash, ya `.env` mein `TEXT_MODEL`) se hoti hai — sirf script badalti hai, alfaaz nahi.
- **Behtar SRT timing:** captions sirf bolne wale hisson par baante jaate hain aur asli pauses par badalte hain.
- **Saare TTS models:** ↻ Refresh Google catalog ke saare TTS models laata hai (list browser mein yaad rehti hai).
- **Progress + Roko:** lambi script par progress bar. Roko ya error ke baad dobara Generate karo to bane hue hisse cache se dobara use hote hain.
- **Rate limit:** saari keys limit par hon to app `CHUNK_DELAY_SEC` intezar karke wahi hissa dobara try karti hai (`RATE_LIMIT_RETRIES` dafa).

## Chalane ka tareeqa (Windows)
1. Python 3.10+ install hona chahiye (`python --version` se check karein).
2. `start.bat` par double-click karein. Packages install honge aur browser khul jayega.
3. Settings mein **Gemini API keys → +** dabayein, key paste karein aur **Key add karein** dabayein. Pehli key add hote hi app tayyar hai; restart nahi chahiye.
4. Mazeed keys isi + button se add karein. **Use** se active key manually select aur **×** se remove karein.

## Keys aur models
- Active key successful ho to wahi rehti hai. Invalid/expired/restricted credential ya quota/rate-limit par TTS request next key se retry hoti hai, last key ke baad first key try hoti hai.
- Ek operation mein har key sirf ek baar try hoti hai. Saari keys unavailable hon to clear error milta hai; quota reset ya key update ke baad dobara Generate karein.
- Failover poori unchanged script ko dobara bhejta hai; adhoori audio ko beech se resume nahi karta. Frontend mein switch notice aur key status update hota hai.
- Manual selection nayi requests par lagti hai. Pehle se chalti request apni captured key order use karti hai.
- **Refresh models** Google ke paginated catalog se saare TTS model IDs load karta hai. Catalog mein hona access/quota ki guarantee nahi. Gemini 2.5 models legacy request format use karte hain; custom/cloned voice ke liye Gemini 3.8 use karein.
- Keys local `.api_keys.json` mein plain text save hoti hain; browser/localStorage mein save nahi hoti. UI/API sirf masked keys dikhate hain. Is file aur `.env` ko share na karein. Existing `.env` key pehli baar import hoti hai.
- Google project ki voice doosre project ki key se accessible na ho sakti hai. Aise model/voice errors par keys ko bekaar mein rotate nahi kiya jata.
- **Voice Agent model:** `LIVE_MODEL` khaali ho to app Google catalog se woh models leti hai jo aapki key ke liye Live API (`bidiGenerateContent`) support karte hain, aur best wala chalati hai. Kisi model par 404 aaye to agla try hota hai. Kaunse models milte hain yeh `/api/live-models` par dekh sakte hain. Agent ke connect hone par status mein model ka naam dikhta hai.
- Reference: https://ai.google.dev/gemini-api/docs/speech-generation and https://ai.google.dev/api/models

## Use
- **Step 1:** Reference audio (10-30 sec, normal bolna) aur consent line record karein -> "Voice clone karo".
  Consent line bilkul isi tarah bolni hai:
  > I am the owner of this voice and I consent to Google using this voice to create a synthetic voice model.
- **Step 2:** Voice select karein, script paste karein -> "Voiceover banao".
  Files `outputs/` folder mein save hoti hain.

## Note
- Generate script ko "Voice consistency" size ke hisab se hisson mein bhejta hai (0 = ek request). Key/quota failure par next key se retry hoti hai; script/model errors seedha dikhte hain. "Paragraphs alag alag banayein" mein har paragraph ke takes alag bante hain.
- Tests: `python -m unittest test_audio_subtitles test_full_script test_dialogue`
- Cloned voice Google project mein 1 saal tak save rehti hai; "Meri voices" se delete kar sakte hain.
- Free tier ki terms mein Google data ko product improvement ke liye use kar sakta hai - apni awaz upload karne se pehle terms dekh lein.
- `.env` file kisi ko share na karein, is mein aapki API key hai.

## Speaker assignment aur language
- Settings ka **Speaking language / accent** dropdown Studio, preview aur Dialogue par apply hota hai. Default Urdu hai. Voice-library search ka language filter alag hai.
- Selected language ki matching regional voices pehle automatically select hoti hain. Catalog successfully load ho kar khaali aaye to standard voices manually select kar sakte hain. Catalog error par automatic fallback nahi hota. Manual choices refresh par preserve hoti hain, aur selected language speaker configuration mein bheji jati hai. Manual voice ka regional accent guaranteed nahi. Custom voices ke liye Auto mode available hai; Auto accent enforce nahi karta.
- Dialogue ki har nonempty line `Ali [happy]: baat` ya `Sara: baat` format mein honi chahiye. Unlabeled lines, unknown/duplicate speaker names aur same voice assignment reject hoti hai.
- Har turn ka speaker metadata uski selected voice se bind hota hai. Poori dialogue ek TTS request mein jati hai (key failure par unchanged retry ho sakti hai).
- Emotions style metadata mein aur vocal effects transcript mein rehte hain. Identity/accent badalne ke long prompt instructions add nahi kiye jate, Google ki voice-consistency guidance ke mutabiq.
- Regional voice selection accent ko support karti hai; generated audio mein perfect accent/speaker consistency ki guarantee nahi. Roman Urdu ambiguous ho to Urdu script use karna pronunciation mein madad kar sakta hai. Actual output sun kar review karein.
