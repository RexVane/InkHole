package main

import "testing"

func TestPetSizeFromIconBaseMatchesLegacyRounding(t *testing.T) {
	tests := []struct {
		iconBase int
		want     int
	}{
		{iconBase: 43, want: 64},
		{iconBase: 64, want: 96},
		{iconBase: 45, want: 68},
		{iconBase: 47, want: 70},
	}

	for _, test := range tests {
		if got := petSizeFromIconBase(test.iconBase); got != test.want {
			t.Errorf("petSizeFromIconBase(%d) = %d, want %d", test.iconBase, got, test.want)
		}
	}
}
