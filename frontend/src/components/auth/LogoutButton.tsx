import { useAuth0 } from "@auth0/auth0-react";
import styled from "styled-components";

const StyledButton = styled.button`
  padding: 10px 20px;
  background-color: #4a5568; /* Dark gray for logout button */
  border: none;
  border-radius: 6px;
  color: #fff;
  font-size: 16px;
  cursor: pointer;
  transition: background-color 0.2s ease;

  &:hover {
    background-color: #2d3748;
  }
`;

export const LogoutButton = () => {
  const { logout } = useAuth0();

  return (
    <StyledButton
      onClick={() =>
        logout({
          logoutParams: {
            returnTo: window.location.origin,
          },
        })
      }
    >
      Log Out
    </StyledButton>
  );
};
