#!/usr/bin/env node
/* Render frontend/next/icons/icon.svg to the PNG sizes the PWA manifest and
   iOS apple-touch-icon need. The PNGs are committed; rerun this only when the
   SVG changes.

   Usage:  node frontend/make_icons.js
   Needs Playwright with a Chromium (preinstalled in the cloud sessions; locally
   `npm i -g playwright && npx playwright install chromium`). */
const path = require("path");
const fs = require("fs");
const { chromium } = require("playwright");

const DIR = path.join(__dirname, "next", "icons");
const SIZES = { "apple-touch-icon.png": 180, "icon-192.png": 192, "icon-512.png": 512 };

(async () => {
  const svg = fs.readFileSync(path.join(DIR, "icon.svg"), "utf8");
  const browser = await chromium.launch();
  for (const [name, px] of Object.entries(SIZES)) {
    const page = await browser.newPage({ viewport: { width: px, height: px } });
    await page.setContent(
      `<style>html,body{margin:0}svg{display:block;width:${px}px;height:${px}px}</style>${svg}`);
    await page.screenshot({ path: path.join(DIR, name), omitBackground: false });
    await page.close();
    console.log(`make_icons: wrote ${name} (${px}x${px})`);
  }
  await browser.close();
})();
