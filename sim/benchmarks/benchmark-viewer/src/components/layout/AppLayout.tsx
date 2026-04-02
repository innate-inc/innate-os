import styled from 'styled-components';

export const AppLayout = styled.div`
  display: grid;
  grid-template-columns: 250px 1fr;
  min-height: 100vh;
  background: ${({ theme }) => theme.colors.background};
`;

export const Sidebar = styled.aside`
  background: ${({ theme }) => theme.colors.surface};
  padding: ${({ theme }) => theme.spacing.lg};
  border-right: 1px solid ${({ theme }) => theme.colors.border};
`;

export const MainContent = styled.main`
  padding: ${({ theme }) => theme.spacing.xl};
  overflow-y: auto;
`;