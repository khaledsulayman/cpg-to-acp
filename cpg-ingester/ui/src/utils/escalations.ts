import type { RunDetail } from '../api/types';

export interface RecommendationAlert {
  id: string;
  title: string;
  reason?: string;
  errors?: string[];
  scope: 'recommendation' | 'section';
}

export function recommendationAlerts(run: RunDetail): RecommendationAlert[] {
  const alerts: RecommendationAlert[] = [];

  for (const recommendation of run.recommendations ?? []) {
    if (!recommendation.escalated) continue;

    const errors = recommendation.escalation_errors ?? [];
    alerts.push({
      id: recommendation.id,
      title: 'Escalated for human review',
      reason: recommendation.escalation_reason || undefined,
      ...(errors.length > 0 ? { errors } : {}),
      scope: 'recommendation',
    });
  }

  for (const [index, item] of (run.recommendationEscalations ?? []).entries()) {
    const errors = item.escalation_errors ?? [];
    alerts.push({
      id: item.id ?? `section-${index}`,
      title: item.name,
      reason: item.escalation_reason || undefined,
      ...(errors.length > 0 ? { errors } : {}),
      scope: 'section',
    });
  }

  return alerts;
}
