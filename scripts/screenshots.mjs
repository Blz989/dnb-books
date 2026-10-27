// Usage: node scripts/screenshots.mjs <base_url> <out_dir> <path> [<path> ...]
// Logs in as the seed_demo user and saves <out_dir>/<slug>-light.png and -dark.png for each path.
// Dev tool only; uses the Playwright that ships with the cloud container.
import { createRequire } from "node:module";
import { mkdirSync } from "node:fs";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const [baseUrl, outDir, ...paths] = process.argv.slice(2);
if (!baseUrl || !outDir || paths.length === 0) {
  console.error("usage: node scripts/screenshots.mjs <base_url> <out_dir> <path> [<path> ...]");
  process.exit(2);
}
mkdirSync(outDir, { recursive: true });

const slug = (p) => (p.replace(/^\/|\/$/g, "").replace(/[^A-Za-z0-9]+/g, "-") || "home");

const browser = await chromium.launch();
try {
  for (const theme of ["light", "dark"]) {
    const context = await browser.newContext({ viewport: { width: 1360, height: 860 }, colorScheme: theme });
    const page = await context.newPage();
    // The login page itself is captured before signing in.
    if (paths.includes("/accounts/login/")) {
      await page.goto(baseUrl + "/accounts/login/");
      await page.evaluate((t) => localStorage.setItem("dnb-theme", t), theme);
      await page.reload();
      await page.screenshot({ path: `${outDir}/${slug("/accounts/login/")}-${theme}.png`, fullPage: true });
    }
    await page.goto(baseUrl + "/accounts/login/");
    await page.fill("#id_username", "demo");
    await page.fill("#id_password", "demo-pass-123");
    await Promise.all([page.waitForNavigation(), page.click("button[type=submit]")]);
    await page.evaluate((t) => localStorage.setItem("dnb-theme", t), theme);
    for (const p of paths) {
      if (p === "/accounts/login/") continue;
      await page.goto(baseUrl + p);
      await page.screenshot({ path: `${outDir}/${slug(p)}-${theme}.png`, fullPage: true });
    }
    await context.close();
  }
} finally {
  await browser.close();
}
