'use client';

import { createContext, useContext } from 'react';

export type Route = { page: string; section?: string; sub?: string; id?: string };

type NavContext = {
  navigate: (route: Route) => void;
  currentRoute: Route;
};

export const DrugOSNavContext = createContext<NavContext>({
  navigate: () => {},
  currentRoute: { page: 'app', section: 'search' },
});

export function useDrugOSNav() {
  return useContext(DrugOSNavContext);
}
