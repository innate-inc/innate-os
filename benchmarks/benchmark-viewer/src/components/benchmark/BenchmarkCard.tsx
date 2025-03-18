import React from 'react';
import styled from 'styled-components';
import { Benchmark } from '../../types/benchmark';

interface BenchmarkCardProps {
  benchmark: Benchmark;
  onClick: () => void;
}

const Card = styled.div`
  background: ${({ theme }) => theme.colors.surface};
  border-radius: ${({ theme }) => theme.borderRadius.large};
  padding: ${({ theme }) => theme.spacing.lg};
  box-shadow: ${({ theme }) => theme.shadows.card};
  cursor: pointer;
  transition: transform 0.2s ease-in-out;
  margin-bottom: ${({ theme }) => theme.spacing.lg};

  &:hover {
    transform: translateY(-2px);
  }
`;

const Title = styled.h2`
  margin: 0 0 ${({ theme }) => theme.spacing.md};
  color: ${({ theme }) => theme.colors.text};
  font-size: 1.2rem;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
`;

const Stats = styled.div`
  display: flex;
  gap: ${({ theme }) => theme.spacing.md};
  margin-bottom: ${({ theme }) => theme.spacing.md};
`;

const Stat = styled.div`
  flex: 1;
  text-align: center;
  background: ${({ theme }) => theme.colors.background};
  padding: ${({ theme }) => theme.spacing.sm};
  border-radius: ${({ theme }) => theme.borderRadius.medium};
`;

const StatValue = styled.div`
  font-size: 1.5rem;
  font-weight: bold;
  color: ${({ theme }) => theme.colors.text};
`;

const StatLabel = styled.div`
  font-size: 0.75rem;
  color: ${({ theme }) => theme.colors.textLight};
  white-space: nowrap;
`;

const SuccessRatio = styled.div<{ ratio: number }>`
  background: ${props => `linear-gradient(90deg, 
    ${props.theme.colors.success} ${props.ratio}%, 
    ${props.theme.colors.error} ${props.ratio}%)`};
  height: 8px;
  border-radius: ${({ theme }) => theme.borderRadius.small};
  margin-top: ${({ theme }) => theme.spacing.sm};
`;

export const BenchmarkCard: React.FC<BenchmarkCardProps> = ({ benchmark, onClick }) => {
  const successRatio = (benchmark.successCount / benchmark.totalTrials) * 100;

  return (
    <Card onClick={onClick}>
      <Title title={benchmark.name}>{benchmark.name}</Title>
      <Stats>
        <Stat>
          <StatValue>{benchmark.totalTrials}</StatValue>
          <StatLabel>Total</StatLabel>
        </Stat>
        <Stat>
          <StatValue>{benchmark.successCount}</StatValue>
          <StatLabel>Success</StatLabel>
        </Stat>
        <Stat>
          <StatValue>{benchmark.totalTrials - benchmark.successCount}</StatValue>
          <StatLabel>Failed</StatLabel>
        </Stat>
      </Stats>
      <SuccessRatio ratio={successRatio} />
    </Card>
  );
};