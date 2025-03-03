import { useAuth0 } from "@auth0/auth0-react";
import styled from "styled-components";

const StyledButton = styled.button`
  padding: 10px 20px;
  background-color: transparent;
  border: 2px solid #6772e5;
  border-radius: ${({ theme }) => theme.borderRadius};
  color: #6772e5;
  font-size: 16px;
  font-family: ${({ theme }) => theme.fonts.body};
  font-weight: ${({ theme }) => theme.fontWeights.medium};
  cursor: pointer;
  transition: all 0.2s ease;

  &:hover {
    background-color: rgba(103, 114, 229, 0.1);
  }
`;

export const SignupButton = () => {
  const { loginWithRedirect } = useAuth0();

  return (
    <StyledButton
      onClick={() =>
        loginWithRedirect({
          authorizationParams: {
            screen_hint: "signup",
          },
        })
      }
    >
      Sign Up
    </StyledButton>
  );
};
