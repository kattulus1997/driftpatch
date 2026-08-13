import {readFileSync} from "node:fs";
import {fileURLToPath} from "node:url";

import sharp from "sharp";

// deslop-ignore-file 33 -- Monospace is limited to machine evidence in this preview.

const fontData = (relativePath) => readFileSync(fileURLToPath(new URL(relativePath, import.meta.url))).toString("base64");
const plex = fontData("../node_modules/@fontsource-variable/ibm-plex-sans/files/ibm-plex-sans-latin-wght-normal.woff2");
const code = fontData("../node_modules/@fontsource/source-code-pro/files/source-code-pro-latin-600-normal.woff2");

// deslop-ignore-next-line 18 -- This is the reproducible social preview, not an icon tile.
const svg = String.raw`<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" viewBox="0 0 1200 630">
  <style>
    @font-face { font-family: Plex; src: url(data:font/woff2;base64,${plex}) format('woff2'); font-weight: 100 900; }
    @font-face { font-family: Code; src: url(data:font/woff2;base64,${code}) format('woff2'); font-weight: 600; }
    .plex { font-family: Plex, sans-serif; }
    .code { font-family: Code, monospace; }
  </style>
  <rect width="1200" height="630" fill="#FBFAF8"/>
  <rect x="38" y="36" width="1124" height="558" rx="14" fill="#FFFFFF" stroke="#DED9D1" stroke-width="2"/>
  <path d="M52 120H1148" stroke="#DED9D1" stroke-width="2"/>
  <path d="M268 36V120" stroke="#DED9D1" stroke-width="2"/>
  <path d="M52 36H268V120H52Z" fill="#F54E00"/>
  <text x="80" y="88" class="plex" font-size="30" font-weight="700" letter-spacing="-1.2" fill="#FFFFFF">DRIFT/PATCH</text>
  <text x="304" y="84" class="plex" font-size="18" font-weight="600" fill="#151515">Public-source repair agent</text>
  <text x="80" y="197" class="plex" font-size="52" font-weight="700" letter-spacing="-1.5" fill="#151515">When the source changes,</text>
  <text x="80" y="255" class="plex" font-size="52" font-weight="700" letter-spacing="-1.5" fill="#151515">the pipeline shouldn't break.</text>

  <rect x="80" y="330" width="1040" height="190" rx="8" fill="#F5F3EF" stroke="#DED9D1"/>
  <path d="M600 330V520" stroke="#DED9D1"/>
  <path d="M80 516H1120" stroke="#F54E00" stroke-width="8"/>
  <text x="120" y="378" class="plex" font-size="15" font-weight="600" fill="#716D67">Baseline</text>
  <text x="640" y="378" class="plex" font-size="15" font-weight="600" fill="#716D67">Observed</text>
  <text x="120" y="446" class="code" font-size="34" fill="#A23822">name</text>
  <text x="640" y="446" class="code" font-size="34" fill="#347943">full_name</text>
  <text x="551" y="446" class="plex" font-size="34" font-weight="600" fill="#F54E00">→</text>

  <text x="80" y="564" class="plex" font-size="16" font-weight="600" fill="#45423E">Bounded repair · deterministic checks · human review</text>
</svg>`;

await sharp(Buffer.from(svg))
  .png()
  .toFile(fileURLToPath(new URL("../public/og-driftpatch.png", import.meta.url)));
