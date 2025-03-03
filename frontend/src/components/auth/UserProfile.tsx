import { useAuth0 } from "@auth0/auth0-react";
import styled from "styled-components";

const ProfileContainer = styled.div`
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px;
`;

const ProfileImage = styled.img`
  width: 40px;
  height: 40px;
  border-radius: 50%;
  object-fit: cover;
`;

const ProfileInfo = styled.div`
  display: flex;
  flex-direction: column;
`;

const UserName = styled.span`
  font-weight: bold;
  font-size: 14px;
`;

const UserEmail = styled.span`
  font-size: 12px;
  color: #666;
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
