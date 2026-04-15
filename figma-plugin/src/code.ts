/// <reference types="@figma/plugin-typings" />

figma.showUI(__html__, { width: 420, height: 580, title: 'The Logo Dominator' });

// ── types ─────────────────────────────────────────────────────────────────────

interface ScaleInfo {
  id: string;
  name: string;
  scale: number;
  vcY: number;
  pixels: number;
  density: number;
}

// ── helpers ───────────────────────────────────────────────────────────────────

function sendSelection(): void {
  const nodes = figma.currentPage.selection;
  figma.ui.postMessage({
    type: 'SELECTION',
    nodes: nodes.map(n => ({ id: n.id, name: n.name })),
  });
}

// ── event handlers ────────────────────────────────────────────────────────────

figma.on('selectionchange', sendSelection);

figma.ui.onmessage = async (msg: { type: string } & Record<string, any>) => {
  switch (msg.type) {

    // UI is ready — send current selection immediately
    case 'READY':
      sendSelection();
      break;

    // Export each selected node as PNG for pixel analysis
    case 'EXPORT': {
      const nodes = figma.currentPage.selection;
      if (!nodes.length) {
        figma.ui.postMessage({ type: 'ERROR', message: 'Select at least one logo on the canvas first.' });
        return;
      }

      const results = await Promise.allSettled(
        nodes.map(async (node) => ({
          id: node.id,
          name: node.name,
          bytes: Array.from(
            await node.exportAsync({ format: 'PNG', constraint: { type: 'HEIGHT', value: 400 } })
          ),
        }))
      );

      const exports = results
        .filter((r): r is PromiseFulfilledResult<{ id: string; name: string; bytes: number[] }> => r.status === 'fulfilled')
        .map(r => r.value);

      figma.ui.postMessage({
        type: 'PNG_DATA',
        exports,
        failed: nodes.length - exports.length,
      });
      break;
    }

    // Receive computed scales, clone + resize nodes, arrange in a new frame
    case 'APPLY_SCALES': {
      const scales: ScaleInfo[] = msg.scales;
      const COLS = 4, GAP = 60, ROW_GAP = 80, PADDING = 50;

      // Reference height = median of original node heights.
      // All clones resize to (REF_HEIGHT × scale), maintaining aspect ratio.
      // This mirrors the web app's REF_H approach so logos scale relative to
      // each other — not relative to their wildly varying Figma canvas sizes.
      const origHeights = scales
        .map(s => { const n = figma.getNodeById(s.id); return n ? (n as any).height as number : 0; })
        .filter((h: number) => h > 0)
        .sort((a: number, b: number) => a - b);
      const REF_HEIGHT: number = origHeights[Math.floor(origHeights.length / 2)] ?? 100;

      const dominated: Array<{ node: SceneNode; vcY: number }> = [];

      for (const s of scales) {
        const original = figma.getNodeById(s.id) as SceneNode | null;
        if (!original || !('clone' in original)) continue;

        const clone = (original as any).clone() as SceneNode;
        const ow: number = (clone as any).width;
        const oh: number = (clone as any).height;
        if ('resize' in clone && oh > 0) {
          const finalH = REF_HEIGHT * s.scale;
          const finalW = (ow / oh) * finalH;
          (clone as any).resize(finalW, finalH);
        }
        dominated.push({ node: clone, vcY: s.vcY });
      }

      if (!dominated.length) {
        figma.ui.postMessage({ type: 'ERROR', message: 'Could not clone any of the selected nodes.' });
        return;
      }

      // Split into rows of COLS
      const nCols = Math.min(COLS, dominated.length);
      const rows: typeof dominated[] = [];
      for (let i = 0; i < dominated.length; i += nCols) {
        rows.push(dominated.slice(i, i + nCols));
      }

      // Column widths: max logo width per column index
      const colWidths = new Array<number>(nCols).fill(0);
      rows.forEach(row =>
        row.forEach((d, j) => {
          colWidths[j] = Math.max(colWidths[j], (d.node as any).width as number);
        })
      );

      // Row heights: tallest logo per row
      const rowHeights = rows.map(row =>
        Math.max(...row.map(d => (d.node as any).height as number))
      );

      const totalW = colWidths.reduce((a, b) => a + b, 0) + GAP * (nCols - 1) + 2 * PADDING;
      const totalH = rowHeights.reduce((a, b) => a + b, 0) + ROW_GAP * (rows.length - 1) + 2 * PADDING;

      // Create the output frame
      const frame = figma.createFrame();
      frame.name = 'Dominated Logos';
      frame.resize(totalW, totalH);
      frame.fills = [{ type: 'SOLID', color: { r: 1, g: 1, b: 1 } }];

      // Precompute column x positions
      const colX: number[] = [];
      let cx = PADDING;
      colWidths.forEach(cw => { colX.push(cx); cx += cw + GAP; });

      // Position and append each clone
      let rowY = PADDING;
      rows.forEach((row, r) => {
        const rowH = rowHeights[r];
        row.forEach((d, j) => {
          const n = d.node as any;
          const lx = colX[j] + (colWidths[j] - n.width) / 2;
          let ly = rowY + rowH / 2 - n.height * d.vcY;
          ly = Math.max(rowY, Math.min(ly, rowY + rowH - n.height));
          n.x = lx;
          n.y = ly;
          frame.appendChild(d.node);
        });
        rowY += rowH + ROW_GAP;
      });

      // Add frame to canvas, select it, zoom in
      figma.currentPage.appendChild(frame);
      figma.currentPage.selection = [frame];
      figma.viewport.scrollAndZoomIntoView([frame]);

      figma.ui.postMessage({ type: 'DONE' });
      break;
    }

    case 'CLOSE':
      figma.closePlugin();
      break;
  }
};
