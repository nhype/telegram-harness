# Trailer

A 60-second trailer for telegram-harness, in English and Russian:

- [telegram-harness-trailer-en.mp4](telegram-harness-trailer-en.mp4)
- [telegram-harness-trailer-ru.mp4](telegram-harness-trailer-ru.mp4)

The video is code. [`trailer/`](trailer) is a [HyperFrames](https://github.com/heygen-com/hyperframes)
project: one HTML composition (`trailer/index.html`) animated with GSAP. The English and Russian copy
live in one dictionary, and the `lang` variable picks the language at render time.

## Render it yourself

Needs Node.js 22+, FFmpeg and Chrome.

```bash
cd demo/trailer
./render.sh          # both languages into demo/
./render.sh ru       # one language
npx --yes hyperframes@0.8.125 preview   # live preview in the browser
```

Rendering is much faster with `chrome-headless-shell`
(`npx @puppeteer/browsers install chrome-headless-shell@stable`, then point
`HYPERFRAMES_BROWSER_PATH` at the binary).

To change the text, edit the `COPY` dictionary at the bottom of `trailer/index.html`.

## Credits

- Fonts: Oswald, Cormorant Garamond, Inter and JetBrains Mono, all under the SIL Open Font License
  (`trailer/assets/fonts/OFL-*.txt`). `fetch_fonts.py` downloads them, `inline_fonts.py` writes their
  `@font-face` rules into the composition.
- Music: synthesized by `trailer/assets/audio/gen_bed.py`. No samples are used.
- Sound effects: from Pixabay under the Pixabay Content License, bundled with HyperFrames
  (`trailer/assets/audio/SFX-CREDITS.md`).
- Film grain: the HyperFrames `grain-overlay` registry component.
