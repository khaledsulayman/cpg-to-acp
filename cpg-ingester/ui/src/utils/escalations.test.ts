import { describe, expect, it } from 'vitest';
import type { RunDetail } from '../api/types';
import { recommendationAlerts } from './escalations';

const makeRun = (overrides: Partial<RunDetail> = {}): RunDetail => ({
  id: 'run-1',
  status: 'awaiting_artifact_review',
  cpgName: 'Test CPG',
  createdAt: '2026-01-01T00:00:00Z',
  steps: [],
  ...overrides,
});

describe('recommendationAlerts', () => {
  it('includes escalation details for an escalated recommendation', () => {
    const alerts = recommendationAlerts(makeRun({
      recommendations: [{
        id: 'rec-1',
        source_cpg: 'CPG-1',
        escalated: true,
        escalation_reason: 'reviewer-unparseable',
        escalation_errors: ['Reviewer response could not be parsed'],
        title: 'Recommendation one',
        content: 'Content',
        recommendation_type: 'clinical',
      }],
    }));

    expect(alerts).toEqual([{
      id: 'rec-1',
      title: 'Escalated for human review',
      reason: 'reviewer-unparseable',
      errors: ['Reviewer response could not be parsed'],
      scope: 'recommendation',
    }]);
  });

  it('includes section escalations when there are no recommendations', () => {
    const alerts = recommendationAlerts(makeRun({
      recommendations: [],
      recommendationEscalations: [{
        id: 'section-4',
        name: 'Section: Pharmacological Treatment',
        type: 'recommendation',
        escalation_reason: 'no-source-text',
      }],
    }));

    expect(alerts).toEqual([{
      id: 'section-4',
      title: 'Section: Pharmacological Treatment',
      reason: 'no-source-text',
      scope: 'section',
    }]);
  });

  it('omits empty error lists', () => {
    const alerts = recommendationAlerts(makeRun({
      recommendations: [{
        id: 'rec-1',
        source_cpg: 'CPG-1',
        escalated: true,
        title: 'Recommendation one',
        content: 'Content',
        recommendation_type: 'clinical',
      }],
    }));

    expect(alerts[0]).not.toHaveProperty('errors');
  });

  it('returns no alerts for a clean run', () => {
    expect(recommendationAlerts(makeRun({
      recommendations: [{
        id: 'rec-1',
        source_cpg: 'CPG-1',
        title: 'Recommendation one',
        content: 'Content',
        recommendation_type: 'clinical',
      }],
    }))).toEqual([]);
  });
});
