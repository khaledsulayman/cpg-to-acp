import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { DmnModdle } from 'dmn-moddle';
import { describe, expect, it } from 'vitest';

import {
  DMN13_FEEL_NS,
  DMN13_MODEL_NS,
  DMN14_FEEL_NS,
  DMN14_MODEL_NS,
  toViewerXml,
} from './dmnViewerCompat';

const testDirectory = dirname(fileURLToPath(import.meta.url));
const goldenDirectory = resolve(testDirectory, '../../../data/golden');

describe('toViewerXml', () => {
  it('rewrites only the DMN model and FEEL namespaces', () => {
    const source = [
      `<definitions xmlns="${DMN14_MODEL_NS}"`,
      `  xmlns:dmn="${DMN14_MODEL_NS}"`,
      `  xmlns:feel="${DMN14_FEEL_NS}"`,
      '  xmlns:dmndi="https://www.omg.org/spec/DMN/20191111/DMNDI/"',
      '  xmlns:dc="http://www.omg.org/spec/DMN/20180521/DC/"',
      '  xmlns:acp="https://redhat.com/cpg-to-acp/dmn"',
      '  namespace="https://redhat.com/cpg-to-acp/dmn/example">',
      '</definitions>',
    ].join('\n');

    const converted = toViewerXml(source);

    expect(converted).toBe(
      source
        .split(DMN14_MODEL_NS).join(DMN13_MODEL_NS)
        .split(DMN14_FEEL_NS).join(DMN13_FEEL_NS),
    );
    expect(converted).toContain(`xmlns:dmndi="https://www.omg.org/spec/DMN/20191111/DMNDI/"`);
    expect(converted).toContain('xmlns:dc="http://www.omg.org/spec/DMN/20180521/DC/"');
    expect(converted).toContain('xmlns:acp="https://redhat.com/cpg-to-acp/dmn"');
    expect(converted).toContain('namespace="https://redhat.com/cpg-to-acp/dmn/example"');
    expect(converted).not.toContain(DMN14_MODEL_NS);
    expect(converted).not.toContain(DMN14_FEEL_NS);
  });

  it('returns DMN 1.3 XML byte-identically', () => {
    const xml = `<definitions xmlns="${DMN13_MODEL_NS}" xmlns:feel="${DMN13_FEEL_NS}"></definitions>`;

    expect(toViewerXml(xml)).toBe(xml);
  });

  it('is idempotent', () => {
    const xml = `<definitions xmlns="${DMN14_MODEL_NS}" xmlns:feel="${DMN14_FEEL_NS}"></definitions>`;
    const converted = toViewerXml(xml);

    expect(toViewerXml(converted)).toBe(converted);
  });
});

describe('DMN 1.4 viewer compatibility', () => {
  it.each([
    ['treatment-recommendation.dmn', 9],
    ['ldl-lowering-risk-category.dmn', 7],
    ['glycemic-escalation-monitoring.dmn', 3],
  ])('%s imports after namespace remapping', async (filename, expectedRuleCount) => {
    const raw = readFileSync(resolve(goldenDirectory, filename), 'utf8');
    const moddle = new DmnModdle();

    await expect(moddle.fromXML(raw)).rejects.toThrow(/failed to parse document as <dmn:Definitions>/i);

    const { rootElement, warnings } = await moddle.fromXML(toViewerXml(raw));
    const decisions = rootElement.drgElement.filter(
      (element) => element.$type === 'dmn:Decision',
    );
    const ruleCount = decisions.reduce(
      (count, decision) => count + (decision.decisionLogic?.rule?.length ?? 0),
      0,
    );

    expect(warnings).toHaveLength(0);
    expect(ruleCount).toBe(expectedRuleCount);
  });
});
