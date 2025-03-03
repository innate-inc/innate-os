import { useAuth0 } from "@auth0/auth0-react";
import styled from "styled-components";

const ProfileContainer = styled.div`
  display: flex;
  align-items: center;
  gap: 8px;
`;

const ProfileImage = styled.img`
  width: 30px;
  height: 30px;
  border-radius: 50%;
  border: 1px solid ${({ theme }) => theme.colors.border};
`;

const ProfileInfo = styled.div`
  display: flex;
  flex-direction: column;
  align-items: flex-start;
`;

const UserName = styled.span`
  font-size: 13px;
  font-weight: ${({ theme }) => theme.fontWeights.medium};
`;

const UserEmail = styled.span`
  font-size: 11px;
  color: ${({ theme }) => theme.colors.muted};
`;

export const UserProfile = () => {
  const { user, isAuthenticated, isLoading } = useAuth0();

  if (isLoading) {
    return <div>Loading...</div>;
  }

  if (!isAuthenticated || !user) {
    return null;
  }

  return (
    <ProfileContainer>
      {user.picture && (
        <ProfileImage src={user.picture} alt={user.name || "User"} />
      )}
      <ProfileInfo>
        <UserName>{user.name}</UserName>
        <UserEmail>{user.email}</UserEmail>
      </ProfileInfo>
    </ProfileContainer>
  );
};
