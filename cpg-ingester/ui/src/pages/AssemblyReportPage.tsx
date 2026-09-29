import {
  Alert,
  Card,
  CardBody,
  CardTitle,
  Content,
  DescriptionList,
  DescriptionListDescription,
  DescriptionListGroup,
  DescriptionListTerm,
  ExpandableSection,
  Split,
  SplitItem,
} from '@patternfly/react-core';
import { useState } from 'react';
import type { RunDetail } from '../api/types';

interface AssemblyReportPageProps {
  run: RunDetail;
}

export function AssemblyReportPage({ run }: AssemblyReportPageProps) {
  const [errorsExpanded, setErrorsExpanded] = useState(false);

  if (!run.assemblyReport) {
    return (
      <Card style={{ marginTop: 16 }}>
        <CardBody>
          <Content component="p">Assembly report not available yet.</Content>
        </CardBody>
      </Card>
    );
  }

  const report = run.assemblyReport;

  return (
    <div style={{ marginTop: 16 }}>
      <Split hasGutter>
        <SplitItem isFilled>
          <Card>
            <CardTitle>Summary</CardTitle>
            <CardBody>
              <DescriptionList isHorizontal isCompact>
                <DescriptionListGroup>
                  <DescriptionListTerm>Recommendations</DescriptionListTerm>
                  <DescriptionListDescription>{report.recommendations_count}</DescriptionListDescription>
                </DescriptionListGroup>
                <DescriptionListGroup>
                  <DescriptionListTerm>DMN Models</DescriptionListTerm>
                  <DescriptionListDescription>{report.dmn_models_count}</DescriptionListDescription>
                </DescriptionListGroup>
                <DescriptionListGroup>
                  <DescriptionListTerm>Escalated Items</DescriptionListTerm>
                  <DescriptionListDescription>{report.escalated_count}</DescriptionListDescription>
                </DescriptionListGroup>
                <DescriptionListGroup>
                  <DescriptionListTerm>Integrity Errors</DescriptionListTerm>
                  <DescriptionListDescription>{report.integrity_errors.length}</DescriptionListDescription>
                </DescriptionListGroup>
              </DescriptionList>
            </CardBody>
          </Card>
        </SplitItem>
      </Split>

      {run.escalatedItems && run.escalatedItems.length > 0 && (
        <Card style={{ marginTop: 16 }}>
          <CardTitle>Escalated Items</CardTitle>
          <CardBody>
            <Content component="p">
              These items need human review — the automated reviewers could not verify them.
            </Content>
            {run.escalatedItems.map((item, i) => (
              <Alert
                key={i}
                variant="warning"
                title={`${item.type}: ${item.name}`}
                isInline
                style={{ marginBottom: 8 }}
              >
                {item.section && <span>Section: {item.section}</span>}
                {item.escalation_reason && <span> — {item.escalation_reason}</span>}
                {item.escalation_errors?.length ? (
                  <ul>
                    {item.escalation_errors.map((error, errorIndex) => (
                      <li key={errorIndex}>{error}</li>
                    ))}
                  </ul>
                ) : null}
              </Alert>
            ))}
          </CardBody>
        </Card>
      )}

      {report.integrity_errors.length > 0 && (
        <Card style={{ marginTop: 16 }}>
          <CardTitle>Integrity Errors</CardTitle>
          <CardBody>
            <ExpandableSection
              toggleText={errorsExpanded ? 'Hide errors' : `Show ${report.integrity_errors.length} error(s)`}
              isExpanded={errorsExpanded}
              onToggle={(_e, expanded) => setErrorsExpanded(expanded)}
            >
              {report.integrity_errors.map((err, i) => (
                <Alert key={i} variant="danger" title={err} isInline isPlain style={{ marginBottom: 4 }} />
              ))}
            </ExpandableSection>
          </CardBody>
        </Card>
      )}
    </div>
  );
}
