package controller

import (
	"testing"
	"time"
	model "todo/pkg/models"

	"github.com/stretchr/testify/assert"
)

func TestAddTask(t *testing.T) {
	tasks := []model.Task{}
	expectedTasks := AddTask("test1", "Test 1 description", tasks)

	if len(expectedTasks) != 1 {
		t.Fatalf("Expected %d tasks but got %d", 1, len(expectedTasks))
	}
}


func TestNewTask(t *testing.T){
	cases := []struct{
		name string
		desc string

		expectedValue model.Task
	}{{
		name: "task1",
		desc: "decription for task 1",
		expectedValue: model.Task{
			Name: "task1",
			Description: "decription for task 1",
			Done:         false,
			DateCreated:  time.Now(),
			DateFinished: time.Time{}},
		},
	{
		name: "task2",
		desc: "decription for task 2",
		expectedValue: model.Task{
			Name: "task2",
			Description: "decription for task 2",
			Done:         false,
			DateCreated:  time.Now(),
			DateFinished: time.Time{}},
	},
}

for _, cas := range cases{
	task := newTask(cas.name, cas.desc)
	assert.Equal(t, task.Name, cas.expectedValue.Name)
	assert.Equal(t, task.Description, cas.expectedValue.Description)
	assert.Equal(t, task.Done, cas.expectedValue.Done)
}

}