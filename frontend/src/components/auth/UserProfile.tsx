import { useAuth0 } from "@auth0/auth0-react";
import styled from "styled-components";
import { useEffect, useState } from "react";

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

// Default avatar as a base64 string or you can use a local image path
const DEFAULT_AVATAR =
  "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCIgZmlsbD0iI2ZmZiI+PHBhdGggZD0iTTEyIDJDNi40OCAyIDIgNi40OCAyIDEyczQuNDggMTAgMTAgMTAgMTAtNC40OCAxMC0xMFMxNy41MiAyIDEyIDJ6bTAgM2MyLjY3IDAgOCAyLjEzIDggN3YyYzAgMi4xMy00LjU4IDQtOCA0cy04LTEuODctOC00di0yYzAtNC44NyA1LjMzLTcgOC03eiIvPjxjaXJjbGUgY3g9IjEyIiBjeT0iOCIgcj0iMiIvPjwvc3ZnPg==";

export const UserProfile = () => {
  const { user, isAuthenticated, isLoading } = useAuth0();
  const [imgSrc, setImgSrc] = useState<string>("");

  useEffect(() => {
    if (user?.picture) {
      setImgSrc(user.picture);
    }
  }, [user]);

  if (isLoading) {
    return <div>Loading...</div>;
  }

  if (!isAuthenticated || !user) {
    return null;
  }

  return (
    <ProfileContainer>
      <ProfileImage
        src={imgSrc || DEFAULT_AVATAR}
        alt={user.name || "User"}
        onError={() => {
          // console.error("Image failed to load:", e);
          setImgSrc(DEFAULT_AVATAR);
        }}
      />
      <ProfileInfo>
        <UserName>{user.name}</UserName>
        <UserEmail>{user.email}</UserEmail>
      </ProfileInfo>
    </ProfileContainer>
  );
};
