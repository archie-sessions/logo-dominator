import * as esbuild from 'esbuild';
import { readFileSync, mkdirSync } from 'fs';

mkdirSync('./dist', { recursive: true });

const html = readFileSync('./src/ui.html', 'utf8');
const watch = process.argv.includes('--watch');

const ctx = await esbuild.context({
  entryPoints: ['./src/code.ts'],
  bundle: true,
  outfile: './dist/code.js',
  target: 'es6',
  define: { __html__: JSON.stringify(html) },
});

if (watch) {
  await ctx.watch();
  console.log('Watching for changes…');
} else {
  await ctx.rebuild();
  await ctx.dispose();
  console.log('Build complete → dist/code.js');
}
