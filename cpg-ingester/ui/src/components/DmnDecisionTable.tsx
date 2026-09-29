import { useEffect, useRef, useState } from 'react';
import DmnViewer from 'dmn-js';
import { Alert } from '@patternfly/react-core';

import 'dmn-js/dist/assets/dmn-js-shared.css';
import 'dmn-js/dist/assets/dmn-js-decision-table.css';
import 'dmn-js/dist/assets/dmn-js-decision-table-controls.css';
import 'dmn-js/dist/assets/dmn-font/css/dmn-embedded.css';
import { toViewerXml } from '../utils/dmnViewerCompat';

interface DmnDecisionTableProps {
  xml: string;
}

export function DmnDecisionTable({ xml }: DmnDecisionTableProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const viewerRef = useRef<InstanceType<typeof DmnViewer> | null>(null);
  const [importError, setImportError] = useState<string | null>(null);

  useEffect(() => {
    if (!containerRef.current) return;

    const viewer = new DmnViewer({
      container: containerRef.current,
    });
    viewerRef.current = viewer;

    return () => {
      viewer.destroy();
      viewerRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (!viewerRef.current || !xml) return;

    viewerRef.current.importXML(toViewerXml(xml)).then(({ warnings }) => {
      setImportError(null);
      if (warnings?.length) {
        console.warn('DMN import warnings:', warnings);
      }
    }).catch((err: Error) => {
      console.error('DMN import failed:', err);
      setImportError(err.message);
    });
  }, [xml]);

  return (
    <>
      {importError && (
        <Alert
          variant="danger"
          title="Decision table could not be rendered"
          isInline
          style={{ marginBottom: 8 }}
        >
          {importError}
        </Alert>
      )}
      <div
        ref={containerRef}
        style={{ width: '100%', minHeight: 200, overflow: 'auto' }}
      />
    </>
  );
}
