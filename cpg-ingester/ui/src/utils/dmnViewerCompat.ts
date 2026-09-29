/**
 * The viewer (dmn-js / dmn-moddle) reads DMN 1.3 only while the pipeline
 * emits DMN 1.4. Decision tables are structurally identical across the two
 * versions, so the viewer copy can remap the model and FEEL namespaces.
 * The original XML is never modified.
 */
export const DMN14_MODEL_NS = 'https://www.omg.org/spec/DMN/20211108/MODEL/';
export const DMN13_MODEL_NS = 'https://www.omg.org/spec/DMN/20191111/MODEL/';
export const DMN14_FEEL_NS = 'https://www.omg.org/spec/DMN/20211108/FEEL/';
export const DMN13_FEEL_NS = 'https://www.omg.org/spec/DMN/20191111/FEEL/';

export function isDmn14(xml: string): boolean {
  return xml.includes(DMN14_MODEL_NS);
}

export function toViewerXml(xml: string): string {
  if (!isDmn14(xml)) return xml;

  return xml
    .split(DMN14_MODEL_NS).join(DMN13_MODEL_NS)
    .split(DMN14_FEEL_NS).join(DMN13_FEEL_NS);
}
