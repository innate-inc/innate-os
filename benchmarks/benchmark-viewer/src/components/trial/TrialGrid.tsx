import React from 'react';
import styled from 'styled-components';
import { Trial } from '../../types/benchmark';

interface TrialListProps {
  trials: Trial[];
  onTrialClick: (trial: Trial) => void;
}

const List = styled.div`
  display: flex;
  flex-direction: column;
  gap: ${({ theme }) => theme.spacing.md};
`;

const TrialRow = styled.div<{ success: boolean }>`
  display: grid;
  grid-template-columns: 80px 100px 120px 120px 1fr;
  gap: ${({ theme }) => theme.spacing.md};
  align-items: center;
  background: ${({ theme }) => theme.colors.surface};
  border-radius: ${({ theme }) => theme.borderRadius.medium};
  padding: ${({ theme }) => theme.spacing.lg};
  cursor: pointer;
  transition: all 0.2s ease-in-out;
  border-left: 4px solid ${({ theme, success }) => 
    success ? theme.colors.success : theme.colors.error};

  &:hover {
    transform: translateX(4px);
    box-shadow: ${({ theme }) => theme.shadows.card};
  }
`;

const TrialId = styled.div`
  font-weight: bold;
  color: ${({ theme }) => theme.colors.text};
`;

const StatusBadge = styled.div<{ success: boolean }>`
  padding: ${({ theme }) => `${theme.spacing.xs} ${theme.spacing.sm}`};
  border-radius: ${({ theme }) => theme.borderRadius.small};
  background: ${({ theme, success }) => 
    success ? theme.colors.success + '20' : theme.colors.error + '20'};
  color: ${({ theme, success }) => 
    success ? theme.colors.success : theme.colors.error};
  font-weight: bold;
  font-size: 0.875rem;
  text-align: center;
`;

const Metric = styled.div`
  text-align: center;
`;

const MetricLabel = styled.div`
  font-size: 0.75rem;
  color: ${({ theme }) => theme.colors.textLight};
  margin-bottom: ${({ theme }) => theme.spacing.xs};
`;

const MetricValue = styled.div`
  font-weight: bold;
  color: ${({ theme }) => theme.colors.text};
`;

const Reason = styled.div`
  color: ${({ theme }) => theme.colors.text};
  font-size: 0.875rem;
  line-height: 1.4;
  overflow: hidden;
  text-overflow: ellipsis;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
`;

const ListHeader = styled.div`
  display: grid;
  grid-template-columns: 80px 100px 120px 120px 1fr;
  gap: ${({ theme }) => theme.spacing.md};
  padding: ${({ theme }) => `${theme.spacing.sm} ${theme.spacing.lg}`};
  color: ${({ theme }) => theme.colors.textLight};
  font-size: 0.875rem;
  font-weight: bold;
  border-bottom: 1px solid ${({ theme }) => theme.colors.border};
  margin-bottom: ${({ theme }) => theme.spacing.md};
`;

export const TrialGrid: React.FC<TrialListProps> = ({ trials, onTrialClick }) => {
  return (
    <List>
      <ListHeader>
        <div>Trial #</div>
        <div>Status</div>
        <div>Duration</div>
        <div>Messages</div>
        <div>Reason</div>
      </ListHeader>
      {trials.map((trial) => (
        <TrialRow
          key={trial.id}
          success={trial.success}
          onClick={() => onTrialClick(trial)}
        >
          <TrialId>#{trial.id}</TrialId>
          <StatusBadge success={trial.success}>
            {trial.success ? 'Success' : 'Failed'}
          </StatusBadge>
          <Metric>
            <MetricLabel>Duration</MetricLabel>
            <MetricValue>{trial.metrics.duration.toFixed(1)}s</MetricValue>
          </Metric>
          <Metric>
            <MetricLabel>Messages</MetricLabel>
            <MetricValue>{trial.metrics.chatMessages}</MetricValue>
          </Metric>
          <Reason title={trial.reason}>
            {trial.reason}
          </Reason>
        </TrialRow>
      ))}
    </List>
  );
};