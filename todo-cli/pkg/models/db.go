package model


import (
	"time"
)

type Task struct {
	Name         string
	Description  string
	Done         bool
	DateCreated  time.Time
	DateFinished time.Time
}


func (t *Task) MarkAsDone(tasks []Task)  []Task{
	t.Done = true
	t.DateFinished = time.Now()

	// Save to DB
	tasks = append(tasks, *t)

	return tasks

}