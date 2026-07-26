package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"time"
)

const (
	repositoryURL = "https://github.com/RexVane/InkHole"
	releasesURL   = repositoryURL + "/releases/latest"
	releasesAPI   = "https://api.github.com/repos/RexVane/InkHole/releases/latest"
)

func (s *InkHoleService) CheckForUpdate() (map[string]any, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, releasesAPI, nil)
	if err != nil {
		return nil, err
	}
	request.Header.Set("Accept", "application/vnd.github+json")
	request.Header.Set("User-Agent", "InkHole/"+appVersion)
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		return nil, fmt.Errorf("检查更新失败: %w", err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("检查更新失败: GitHub 返回 %s", response.Status)
	}
	var release struct {
		TagName string `json:"tag_name"`
		HTMLURL string `json:"html_url"`
		Name    string `json:"name"`
	}
	if err := json.NewDecoder(response.Body).Decode(&release); err != nil {
		return nil, fmt.Errorf("检查更新失败: %w", err)
	}
	latest := strings.TrimSpace(strings.TrimPrefix(release.TagName, "v"))
	if latest == "" {
		return nil, errors.New("检查更新失败: 发布版本无效")
	}
	url := release.HTMLURL
	if !strings.HasPrefix(url, repositoryURL+"/releases/") {
		url = releasesURL
	}
	return map[string]any{
		"current":   appVersion,
		"latest":    latest,
		"available": compareVersions(latest, appVersion) > 0,
		"name":      release.Name,
		"url":       url,
	}, nil
}

func compareVersions(left, right string) int {
	parse := func(value string) []int {
		value = strings.TrimSpace(strings.TrimPrefix(value, "v"))
		if index := strings.IndexAny(value, "-+"); index >= 0 {
			value = value[:index]
		}
		parts := strings.Split(value, ".")
		result := make([]int, len(parts))
		for index, part := range parts {
			result[index], _ = strconv.Atoi(part)
		}
		return result
	}
	a, b := parse(left), parse(right)
	length := max(len(a), len(b))
	for index := 0; index < length; index++ {
		var av, bv int
		if index < len(a) {
			av = a[index]
		}
		if index < len(b) {
			bv = b[index]
		}
		if av < bv {
			return -1
		}
		if av > bv {
			return 1
		}
	}
	return 0
}

func (s *InkHoleService) OpenReleases() error {
	if s.app == nil {
		return errors.New("应用尚未启动")
	}
	return s.app.Browser.OpenURL(releasesURL)
}

func (s *InkHoleService) OpenRepository() error {
	if s.app == nil {
		return errors.New("应用尚未启动")
	}
	return s.app.Browser.OpenURL(repositoryURL)
}
