import { useAuth0 } from "@auth0/auth0-react";
import { LoginButton } from "./LoginButton";
import { LogoutButton } from "./LogoutButton";
import styled from "styled-components";

const ButtonContainer = styled.div`
  display: flex;
  gap: 10px;
`;

export const AuthenticationButton = () => {
  const { isAuthenticated, isLoading } = useAuth0();

  if (isLoading) {
    return <div>Loading...</div>;
  }

  return (
    <ButtonContainer>
      {isAuthenticated ? <LogoutButton /> : <LoginButton />}
    </ButtonContainer>
  );
};
