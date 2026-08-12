import sharp from "sharp";
import {fileURLToPath} from "node:url";

const svg = String.raw`<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" viewBox="0 0 1200 630">
  <rect width="1200" height="630" fill="#f5f2ea"/>
  <path d="M0 84H1200M0 494H1200M400 84V494M800 84V494" stroke="#151515" stroke-width="2"/>
  <text x="42" y="55" font-family="Arial Narrow,Arial,sans-serif" font-size="34" font-weight="900" fill="#151515">DRIFTPATCH</text>
  <text x="310" y="52" font-family="monospace" font-size="15" fill="#151515">PUBLIC DATA REPAIR AGENT</text>
  <text x="42" y="154" font-family="Arial Narrow,Arial,sans-serif" font-size="56" font-weight="900" fill="#151515">When the source changes,</text>
  <text x="42" y="216" font-family="Arial Narrow,Arial,sans-serif" font-size="56" font-weight="900" fill="#151515">the pipeline shouldn't break.</text>
  <text x="42" y="310" font-family="Arial Narrow,Arial,sans-serif" font-size="56" font-weight="900" fill="#151515">01</text>
  <text x="128" y="303" font-family="monospace" font-size="24" font-weight="700" fill="#151515">EVIDENCE</text>
  <text x="442" y="310" font-family="Arial Narrow,Arial,sans-serif" font-size="56" font-weight="900" fill="#151515">02</text>
  <text x="528" y="303" font-family="monospace" font-size="24" font-weight="700" fill="#151515">PATCH</text>
  <text x="842" y="310" font-family="Arial Narrow,Arial,sans-serif" font-size="56" font-weight="900" fill="#151515">03</text>
  <text x="928" y="303" font-family="monospace" font-size="24" font-weight="700" fill="#151515">VERIFY</text>
  <path d="M44 368H335" stroke="#b8b3a8" stroke-width="2"/><path d="M444 368H735" stroke="#ed4b12" stroke-width="5"/><path d="M844 368H1135" stroke="#151515" stroke-width="2"/>
  <text x="42" y="420" font-family="monospace" font-size="18" fill="#68655e">OBSERVE THE DRIFT</text>
  <text x="442" y="420" font-family="monospace" font-size="18" fill="#ed4b12">ONE BOUNDED ACTION</text>
  <text x="842" y="420" font-family="monospace" font-size="18" fill="#68655e">DETERMINISTIC PROOF</text>
  <rect x="42" y="535" width="232" height="58" fill="#ed4b12"/>
  <text x="72" y="572" font-family="monospace" font-size="20" font-weight="700" fill="#fff">10/10 DECISIONS</text>
  <text x="322" y="572" font-family="monospace" font-size="20" fill="#151515">8 REPAIRED</text>
  <text x="550" y="572" font-family="monospace" font-size="20" fill="#151515">2 ESCALATED</text>
  <text x="820" y="572" font-family="monospace" font-size="20" fill="#151515">0 AUTO-MERGES</text>
</svg>`;

await sharp(Buffer.from(svg))
  .png()
  .toFile(fileURLToPath(new URL("../public/og-driftpatch.png", import.meta.url)));
