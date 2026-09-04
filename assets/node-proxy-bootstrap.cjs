'use strict';

const proxyUrl = process.env.ANTIGRAVITY_PROXY_URL;
const proxyBypass = process.env.ANTIGRAVITY_PROXY_BYPASS;

if (proxyUrl) {
  const argv = process.argv.map((value) => String(value));
  const mainEntry = (argv[1] || '').toLowerCase();
  const remaining = argv.slice(2).map((value) => value.toLowerCase());
  const isDirectEntry = mainEntry.includes('chrome-devtools-mcp');
  const isNpxEntry = /(?:^|[\\/])(?:npx-cli|npm-cli)\.js$/.test(mainEntry)
    && remaining.some((value) => value.includes('chrome-devtools-mcp'));

  if (isDirectEntry || isNpxEntry) {
    const hasProxyServer = argv.some((value) => value.startsWith('--proxy-server='));
    const hasProxyBypass = argv.some((value) => value.includes('--proxy-bypass-list='));

    if (!hasProxyServer) {
      process.argv.push(`--proxy-server=${proxyUrl}`);
    }
    if (proxyBypass && !hasProxyBypass) {
      process.argv.push(`--chrome-arg=--proxy-bypass-list=${proxyBypass}`);
    }
  }
}
