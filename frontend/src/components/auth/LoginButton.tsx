import { useAuth0 } from "@auth0/auth0-react";
import styled from "styled-components";

const StyledButton = styled.button`
  padding: 10px 20px;
  background-color: #007bff;
  border: none;
  border-radius: 6px;
  color: #fff;
  font-size: 16px;
  cursor: pointer;
  transition: background-color 0.2s ease;

  &:hover {
    background-color: #0056b3;
  }
`;

export const LoginButton = () => {
  const { loginWithRedirect } = useAuth0();

  return (
    <StyledButton
      onClick={() =>
        loginWithRedirect({
          authorizationParams: {
            prompt: "login",
          },
        })
      }
    >
      Log In
    </StyledButton>
  );
};
